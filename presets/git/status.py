#!/usr/bin/env python3
"""Git status dashboard — where am I, what's changed, what's stashed.

Combines branch info, recent commits, working tree state, and stash
list into one structured report.

Modes (colon-appended: `git-status:full`):
  - (default) — each file/branch/stash list is capped with a `... (N more)`
    marker, keeping the overview cheap.
  - full (alias: porcelain) — uncaps every list for the complete untruncated
    view, e.g. to drive precise staging (excluding a few pre-existing untracked
    items from a large commit) where a truncated list isn't enough.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

# Sibling import: runtime puts this dir on sys.path[0]; the test harness
# loads scripts via importlib (no dir on path), so add it explicitly.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _checks  # noqa: E402  (the one check tally, shared with gh-pr / gh-prs)
from _env import env_int  # noqa: E402  (the one numeric-knob reader)
from _git_common import use_utf8_stdout  # noqa: E402


_GIT_TIMEOUT_DEFAULT = 5

# Shell convention for "killed by a timeout" (coreutils `timeout`). Distinct
# from any exit code git itself produces, so a caller checking `returncode != 0`
# keeps working while a caller that wants to tell the two apart can.
TIMEOUT_RC = 124

INCOMPLETE_MARKER = "git-status INCOMPLETE"

# Every call that could not answer this invocation, as (command, why).
# Module-level because the calls are scattered through `main()` and the note
# can only be written once the last of them has had its chance.
#
# Not only timeouts (#705). A `glab` that refuses for want of a token, or a
# `git status` that meets a held index lock, has answered nothing either, and
# a reader looking at the missing section needs the same disclosure for both.
# The reason travels with the command because "did not answer" alone sends
# them to raise SUPERTOOL_GIT_TIMEOUT for a problem that is an expired token.
_UNANSWERED: list[tuple[str, str]] = []

# The phrases with which `glab` and `gh` say "there is no such request here".
# That is an answer — a fact about the repository — and it renders as silence,
# because a branch with no MR yet is the most ordinary state a branch can be
# in and a footer on every such run would not be read on the run that needs it.
#
# Both CLIs exit 1 for that *and* for an expired token, so the exit code cannot
# tell them apart and the sentence has to. Anything unrecognised is disclosed
# rather than assumed benign: being wrong in that direction costs one footer
# line quoting the CLI's own words, which a reader can dismiss in a second;
# being wrong the other way is the defect this list exists to prevent.
_ANSWERED_NONE = (
    "no open merge request",
    "no merge request",
    "no pull request",
    # The other host's CLI, in a repo that is not on its host: a GitLab repo
    # runs `gh` too, and a GitHub repo runs `glab`. Structural and permanent —
    # nothing about it will be different on the next run.
    "none of the git remotes",
    "no git remotes found",
    "no remotes found",
)


def _git_timeout(default: int | None = None) -> int:
    """Budget for one git call, overridable per environment (#650).

    Same shape and same reasoning as `SUPERTOOL_LINT_TIMEOUT` (#553): a loaded
    runner occasionally needs room without a code change, and what supertool
    ships with does not move for it.
    """
    base = _GIT_TIMEOUT_DEFAULT if default is None else default
    return env_int("SUPERTOOL_GIT_TIMEOUT", base, minimum=1)


def _git(args: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    """Run a git command; a call that does not answer costs its own line (#650).

    `TimeoutExpired` used to escape. One stalled `rev-list` — the courtesy line
    about divergence from master — then took the whole report with it, stack
    trace and all: no branch, no commits, no working tree, no PR. That is the
    loudest possible reaction to the least important call on the page.

    It is not swallowed either. The result carries `TIMEOUT_RC`, so every call
    site's existing `returncode != 0` branch skips its section exactly as it
    would for a git that failed, and the call is recorded so the footer can say
    which sections are missing *because git did not answer* rather than because
    there was nothing to report (docs/validators.md, "Declining instead of
    guessing"). Rendering it as a success is the one thing that must never
    happen here: an empty `rev-list --left-right --count` stdout would print as
    `0 ahead — branch has no own commits!`, a false alarm about the branch
    manufactured out of a fact about the machine.
    """
    budget = _git_timeout(timeout)
    cmd = ["git"] + args
    try:
        return subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=budget, encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        _UNANSWERED.append((" ".join(cmd), f"timed out after {budget}s"))
        return subprocess.CompletedProcess(
            args=cmd, returncode=TIMEOUT_RC, stdout="",
            stderr=f"timed out after {budget}s",
        )


def _reason(returncode: int, stderr: str) -> str:
    """Why a call did not answer, flattened to one line for the footer.

    The CLI's own words, not a paraphrase: "exit 1: error: 401 Unauthorized"
    tells the reader to re-authenticate, where "did not answer" would send
    them to raise a timeout that was never the problem.
    """
    said = " ".join(stderr.split())
    return f"exit {returncode}: {said[:100]}" if said else f"exit {returncode}"


def _note_failed(cmd: list[str], r: subprocess.CompletedProcess[str]) -> None:
    """Disclose a git call that failed for a reason `_git` has not recorded.

    Only for calls with no legitimate non-zero exit — `git status` and `git
    stash list`, inside a repository the branch lookup has already proved
    exists. The calls that fail as a matter of course stay silent on purpose:
    `rev-parse --verify master` on a repo that has neither master nor main,
    `rev-list @{upstream}` on a branch that tracks nothing. Disclosing those
    would put a footer on nearly every run, and a footer on every run is one
    nobody reads on the run that needed it.

    A timeout is already in `_UNANSWERED` with the budget it was cut off at,
    so it is skipped here rather than counted twice.
    """
    if r.returncode in (0, TIMEOUT_RC):
        return
    _UNANSWERED.append(("git " + " ".join(cmd), _reason(r.returncode, r.stderr)))


def _hosted_request(cmd: list[str]) -> dict | None:
    """One `glab mr view` / `gh pr view` JSON lookup — three states, not two.

    Returns the parsed object, or `None` when there is no section to render.
    The two reasons for `None` are not the same thing and are not rendered the
    same way: "this branch has no MR" is left silent, while a lookup that
    stalled, was refused, or answered with something that is not JSON is
    recorded so the `INCOMPLETE` footer names it.

    Both used to be `pass` (#705). A network stall, an expired token and an
    unauthenticated CLI therefore produced byte-for-byte the report of a
    branch that simply has no MR yet — in the file #685 rewrote to carry a
    footer for exactly this, by a route that never reached it.

    A CLI that is not installed stays silent. Nothing on that machine was ever
    going to answer, and a decline that can never resolve is noise on every
    run of every op (docs/validators.md, "Declining instead of guessing") —
    the same reading that keeps a missing `git` binary quiet.
    """
    budget = _git_timeout()
    label = " ".join(cmd[:3])
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=budget,
            encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        _UNANSWERED.append((label, f"timed out after {budget}s"))
        return None
    except OSError as e:
        _UNANSWERED.append((label, f"could not be run: {e}"))
        return None
    if r.returncode != 0:
        said = (r.stderr + r.stdout).lower()
        if not any(phrase in said for phrase in _ANSWERED_NONE):
            _UNANSWERED.append((label, _reason(r.returncode, r.stderr)))
        return None
    try:
        parsed = json.loads(r.stdout)
    except json.JSONDecodeError:
        # Exit 0 and a body that is not JSON is not "there is no MR" either —
        # a proxy interstitial and a CLI version that dropped the flag both
        # land here, and both leave the section unknown rather than empty.
        _UNANSWERED.append((label, "answered with output that is not JSON"))
        return None
    if not isinstance(parsed, dict):
        _UNANSWERED.append((label, "answered with JSON that is not an object"))
        return None
    return parsed


def _incomplete_note() -> str:
    """One line naming the calls that went unanswered, or "" if none."""
    if not _UNANSWERED:
        return ""
    shown = [f"`{c}` ({w})" for c, w in _UNANSWERED[:3]]
    more = len(_UNANSWERED) - len(shown)
    calls = ", ".join(shown) + (f" (+{more} more)" if more else "")
    plural = "s" if len(_UNANSWERED) != 1 else ""
    return (
        f"\n{INCOMPLETE_MARKER} — {len(_UNANSWERED)} call{plural} did not answer "
        f"and {'were' if len(_UNANSWERED) != 1 else 'was'} skipped: {calls}. "
        f"Sections that depend on them are missing because the call did not "
        f"answer, not because there was nothing to report. "
        f"Raise SUPERTOOL_GIT_TIMEOUT if a timeout recurs."
    )


def _head_commit_age_secs(sha: str) -> int | None:
    """Seconds since `sha` was committed, read from the local object store.

    `None` when it cannot be established, which the caller must render as a
    decline — never as either verdict (`_checks.absence`).

    **Zero network calls, deliberately.** `gh-pr` pays a GraphQL lookup for this
    age because it holds only a PR number; `git-status` is standing in the repo,
    and the PR's head commit is almost always already in this object store —
    you are the one who pushed it. That matters more here than in `gh-pr`:
    `git-status` is the most frequently run op in the tool *and* the zero-runs
    leg is its common case, because running it right after a push is the whole
    reason you run it. A network call on that path would be the wrong fix.

    When the object is genuinely absent — someone else pushed the head, or this
    clone never fetched it — the answer is `None`. Substituting the local HEAD's
    date would date a different commit and caption it as the PR's head, which is
    the defect being fixed, moved one layer along.

    Only a full 40-hex object name is accepted: `HEAD` and `master` are valid
    revision arguments that resolve, locally, to the wrong commit.

    Committer date, matching `gh-pr`'s `committedDate` fallback — it can predate
    the push, which only ever makes the age look *older*, and old-and-empty on
    an open PR is `UNKNOWN` rather than proof, so the skew cannot manufacture a
    "none will be created".
    """
    if not _checks.is_full_sha(sha):
        return None
    r = _git(["log", "-1", "--format=%ct", f"{sha}^{{commit}}"], timeout=3)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        committed = int(r.stdout.strip())
    except ValueError:
        return None
    return max(0, int(time.time()) - committed)


def main() -> int:
    use_utf8_stdout()
    # This invocation's tally, not the process's. In production they are the
    # same thing; under a test harness that imports this module once and calls
    # main() repeatedly, a stale entry would caption a clean run as incomplete.
    _UNANSWERED.clear()
    # `git-status:full` (alias `:porcelain`) uncaps every list below — for when
    # the default truncated overview isn't enough to drive precise staging
    # (e.g. excluding a few pre-existing untracked items from a large commit).
    mode = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    full = mode in ("full", "porcelain")

    # 1. Branch + tracking
    branch_result = _git(["branch", "-vv", "--no-color"])
    if branch_result.returncode != 0:
        stderr = branch_result.stderr.lower()
        if "not a git repository" in stderr:
            print("ERROR: not inside a git repository.")
        else:
            print(f"ERROR: git failed: {branch_result.stderr.strip()}")
        return 1

    current_branch = ""
    tracking = ""
    for line in branch_result.stdout.splitlines():
        if line.startswith("* "):
            current_branch = line[2:].strip()
            break

    # Cleaner branch + remote info
    branch_name_result = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    branch_name = branch_name_result.stdout.strip() if branch_name_result.returncode == 0 else "?"

    # Ahead/behind
    ahead_behind = ""
    ab_result = _git(["rev-list", "--left-right", "--count", f"HEAD...@{{upstream}}"])
    if ab_result.returncode == 0:
        parts = ab_result.stdout.strip().split()
        if len(parts) == 2:
            ahead, behind = int(parts[0]), int(parts[1])
            if ahead and behind:
                ahead_behind = f"ahead {ahead}, behind {behind}"
            elif ahead:
                ahead_behind = f"ahead {ahead}"
            elif behind:
                ahead_behind = f"behind {behind}"
            else:
                ahead_behind = "up to date"

    # Divergence from base branch (master/main) — distinct from upstream tracking
    base_divergence = ""
    base_branch = ""
    for candidate in ("master", "main"):
        check = _git(["rev-parse", "--verify", "--quiet", candidate])
        if check.returncode == 0:
            base_branch = candidate
            break
    if base_branch and branch_name != base_branch:
        base_ab = _git(["rev-list", "--left-right", "--count",
                        f"{base_branch}...HEAD"])
        if base_ab.returncode == 0:
            parts = base_ab.stdout.strip().split()
            if len(parts) == 2:
                behind_base, ahead_base = int(parts[0]), int(parts[1])
                if ahead_base == 0:
                    suffix = f", {behind_base} behind" if behind_base else ""
                    base_divergence = (f"vs {base_branch}: 0 ahead{suffix} "
                                       f"— branch has no own commits!")
                else:
                    parts_str = f"{ahead_base} ahead"
                    if behind_base:
                        parts_str += f", {behind_base} behind"
                    base_divergence = f"vs {base_branch}: {parts_str}"

    print(f"# git-status")
    print(f"Branch: {branch_name}" + (f" ({ahead_behind})" if ahead_behind else ""))
    if base_divergence:
        print(base_divergence)

    # Origin HEAD — explicit, so callers don't need raw `git log origin/...`
    origin_head = _git(["log", "-1", "--format=%h %s", "@{upstream}"])
    if origin_head.returncode == 0 and origin_head.stdout.strip():
        print(f"Origin HEAD: {origin_head.stdout.strip()}")

    # Other local branches with unpushed/unpulled work — so a commit made on a
    # branch you're NOT standing on stays visible (classic: committed to master,
    # then checked out a feature branch — the work looks lost from `feature`).
    others = _git(["for-each-ref",
                   "--format=%(refname:short)\t%(upstream:track)", "refs/heads"])
    if others.returncode == 0:
        rows = []
        for line in others.stdout.splitlines():
            name, _, track = line.partition("\t")
            track = track.strip()
            # Only actionable divergence — skip the current branch (covered
            # above) and stale [gone] branches (merged, upstream pruned).
            if name and name != branch_name and ("ahead" in track or "behind" in track):
                # Drop git's surrounding brackets so it reads like the rest of
                # the file: `ahead 1, behind 3`, not `[ahead 1, behind 3]`.
                rows.append((name, track.strip("[]")))
        if rows:
            print("\n## Other branches with unpushed/unpulled work")
            for name, track in (rows if full else rows[:10]):
                print(f"  {name}  {track}")
            if not full and len(rows) > 10:
                print(f"  ... ({len(rows) - 10} more)")

    # 2. Last 5 commits
    log_result = _git(["log", "-5", "--format=%h %ad %an | %s", "--date=short"])
    if log_result.returncode == 0 and log_result.stdout.strip():
        print(f"\n## Last 5 commits")
        for line in log_result.stdout.strip().splitlines():
            print(f"  {line}")

    # 3. Working tree
    status_cmd = ["status", "--porcelain=v1"]
    status_result = _git(status_cmd)
    _note_failed(status_cmd, status_result)
    if status_result.returncode == 0:
        lines = [l for l in status_result.stdout.splitlines() if l.strip()]
        staged = [l for l in lines if l[0] != " " and l[0] != "?"]
        unstaged = [l for l in lines if len(l) > 1 and l[1] != " " and l[0] != "?"]
        untracked = [l for l in lines if l.startswith("??")]

        if not lines:
            print(f"\n## Working tree: clean")
        else:
            print(f"\n## Working tree ({len(lines)} changes)")
            if staged:
                print(f"\n### Staged ({len(staged)})")
                for l in (staged if full else staged[:20]):
                    print(f"  {l}")
                if not full and len(staged) > 20:
                    print(f"  ... ({len(staged) - 20} more)")
            if unstaged:
                print(f"\n### Unstaged ({len(unstaged)})")
                for l in (unstaged if full else unstaged[:20]):
                    print(f"  {l}")
                if not full and len(unstaged) > 20:
                    print(f"  ... ({len(unstaged) - 20} more)")
            if untracked:
                print(f"\n### Untracked ({len(untracked)})")
                for l in (untracked if full else untracked[:10]):
                    print(f"  {l[3:]}")
                if not full and len(untracked) > 10:
                    print(f"  ... ({len(untracked) - 10} more)")

    # 4. Stash
    stash_cmd = ["stash", "list"]
    stash_result = _git(stash_cmd)
    _note_failed(stash_cmd, stash_result)
    if stash_result.returncode == 0 and stash_result.stdout.strip():
        stashes = stash_result.stdout.strip().splitlines()
        print(f"\n## Stashes ({len(stashes)})")
        for s in (stashes if full else stashes[:5]):
            print(f"  {s}")
        if not full and len(stashes) > 5:
            print(f"  ... ({len(stashes) - 5} more)")

    # 5. MR/PR for current branch (try glab, then gh — skip if neither available)
    mr = _hosted_request(["glab", "mr", "view", branch_name, "--output", "json"])
    if mr is not None:
        mr_iid = mr.get("iid", "?")
        mr_title = mr.get("title", "?")
        mr_state = mr.get("state", "?")
        mr_target = mr.get("target_branch", "?")
        pipeline = mr.get("pipeline") or mr.get("head_pipeline") or {}
        if not isinstance(pipeline, dict):
            pipeline = {}
        # A missing pipeline is GitLab's spelling of #585's ambiguity, and
        # `none` renders it as the "never" reading for free. Decline instead
        # — see _checks.NO_PIPELINE for why there is no grace leg here.
        pipe_status = pipeline.get("status") or _checks.NO_PIPELINE

        print(f"\n## MR !{mr_iid} — {mr_title}")
        print(f"State: {mr_state} | Target: {mr_target} | Pipeline: {pipe_status}")

        # MR diff size — file count from existing JSON (no extra network).
        # +/- line counts via local git diff against target branch (also no
        # network; falls back silently if target ref isn't present locally).
        changes_count = mr.get("changes_count")
        if changes_count is None or changes_count == "" or changes_count == "0":
            print("Diff: EMPTY — branch has no commits ahead of target!")
        else:
            diff_line = f"Diff: {changes_count} files"
            target_ref = f"origin/{mr_target}" if mr_target != "?" else ""
            if target_ref:
                shortstat = _git(["diff", "--shortstat",
                                  f"{target_ref}...HEAD"], timeout=3)
                if shortstat.returncode == 0 and shortstat.stdout.strip():
                    # e.g. " 5 files changed, 126 insertions(+), 72 deletions(-)"
                    text = shortstat.stdout.strip()
                    adds = re.search(r"(\d+) insertions?", text)
                    dels = re.search(r"(\d+) deletions?", text)
                    a = adds.group(1) if adds else "0"
                    d = dels.group(1) if dels else "0"
                    diff_line += f" (+{a} -{d})"
            print(diff_line)

        # Extract linked issue from description
        desc = mr.get("description") or ""
        issue_match = re.search(r'#(\d{4,})', desc)
        if issue_match:
            print(f"Issue: #{issue_match.group(1)}")

    # Try GitHub (gh) if glab didn't find anything
    if mr is None:
        pr = _hosted_request(
            ["gh", "pr", "view", branch_name, "--json",
             "number,title,state,baseRefName,statusCheckRollup,body,"
             "additions,deletions,changedFiles,headRefOid,mergeable"])
        if pr is not None:
            pr_num = pr.get("number", "?")
            pr_title = pr.get("title", "?")
            pr_state = pr.get("state", "?")
            pr_target = pr.get("baseRefName", "?")
            # `headRefOid` rides along in the single `gh pr view` call
            # already being made — the field costs nothing extra.
            pr_head = str(pr.get("headRefOid") or "")
            local = _git(["rev-parse", "HEAD"], timeout=3)
            local_head = local.stdout.strip() if local.returncode == 0 else ""

            # Computed before the Checks line, not just for printing after
            # it: `''` means the two SHAs are *established equal* (#587), and
            # that is what decides whether a claim about the PR's merge
            # state may be made about the commit under the reader's cursor.
            relation = _checks.head_relation(local_head, pr_head, pr_num)

            check_states = _checks.github_states(pr.get("statusCheckRollup"))
            if check_states:
                check_summary = _checks.summarize(check_states)
            else:
                # Zero check runs is four states, not one (#585, #594). The
                # evidence is the age of the *PR's* head commit and the PR
                # state; the age comes from the local object store, so this
                # leg pays no network call either. `absence()` also returns
                # a `Mergeable:` suffix so the two lines cannot disagree —
                # `git-status` prints no Mergeable line, so it is dropped.
                #
                # `mergeable` rides the `gh pr view` call already being made
                # (#594), and is withheld unless local HEAD is established
                # equal to the PR head: "CONFLICTING, so rebase" is about a
                # specific commit, and stating it about one the reader has
                # moved past is #587's defect wearing #594's words. Withheld,
                # it falls through to the three legs above unchanged.
                check_summary, _unused_merge_note = _checks.absence(
                    pr_state, _head_commit_age_secs(pr_head),
                    mergeable=pr.get("mergeable") if relation == "" else None,
                )

            print(f"\n## PR #{pr_num} — {pr_title}")
            print(f"State: {pr_state} | Target: {pr_target} | Checks: {check_summary}")
            # Whichever of the two the Checks line came from, it is a
            # statement about the PR's head commit. Say so whenever that is
            # not the commit the reader is standing on (#587).
            if relation:
                print(relation)

            changed_files = pr.get("changedFiles", 0)
            if changed_files == 0:
                print("Diff: EMPTY — branch has no commits ahead of target!")
            else:
                print(f"Diff: {changed_files} files (+{pr.get('additions', 0)} -{pr.get('deletions', 0)})")

            # Linked issue — one shared extractor with `gh-pr`, which
            # carried the identical pattern and the identical defect
            # (#591). The keyword was optional there too, so both printed
            # the first `#N` in the body as the issue being closed.
            print(_checks.linked_issue_line(
                _checks.closing_issue_refs(pr.get("body"))))

    # Last, and only when there is something to disclose. A reader reaching for
    # `| tail` sees it; a clean run carries no permanent disclaimer, which would
    # disclose nothing (#621's footer, same reasoning).
    note = _incomplete_note()
    if note:
        print(note)

    return 0


if __name__ == "__main__":
    sys.exit(main())
