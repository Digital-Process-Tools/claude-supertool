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

import os
import subprocess
import sys
import time

# Sibling import: runtime puts this dir on sys.path[0]; the test harness
# loads scripts via importlib (no dir on path), so add it explicitly.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _checks  # noqa: E402  (the one check tally, shared with gh-pr / gh-prs)
from _git_common import use_utf8_stdout  # noqa: E402


def _git(args: list[str], timeout: int = 5) -> subprocess.CompletedProcess[str]:
    """Run a git command."""
    return subprocess.run(
        ["git"] + args,
        capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
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
    status_result = _git(["status", "--porcelain=v1"])
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
    stash_result = _git(["stash", "list"])
    if stash_result.returncode == 0 and stash_result.stdout.strip():
        stashes = stash_result.stdout.strip().splitlines()
        print(f"\n## Stashes ({len(stashes)})")
        for s in (stashes if full else stashes[:5]):
            print(f"  {s}")
        if not full and len(stashes) > 5:
            print(f"  ... ({len(stashes) - 5} more)")

    # 5. MR/PR for current branch (try glab, then gh — skip if neither available)
    import json as _json
    import re as _re

    mr_found = False

    # Try GitLab (glab)
    try:
        glab_result = subprocess.run(
            ["glab", "mr", "view", branch_name, "--output", "json"],
            capture_output=True, text=True, timeout=5, encoding="utf-8", errors="replace",
        )
        if glab_result.returncode == 0:
            mr = _json.loads(glab_result.stdout)
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
                        adds = _re.search(r"(\d+) insertions?", text)
                        dels = _re.search(r"(\d+) deletions?", text)
                        a = adds.group(1) if adds else "0"
                        d = dels.group(1) if dels else "0"
                        diff_line += f" (+{a} -{d})"
                print(diff_line)

            # Extract linked issue from description
            desc = mr.get("description") or ""
            issue_match = _re.search(r'#(\d{4,})', desc)
            if issue_match:
                print(f"Issue: #{issue_match.group(1)}")
            mr_found = True
    except (FileNotFoundError, subprocess.TimeoutExpired, _json.JSONDecodeError):
        pass

    # Try GitHub (gh) if glab didn't find anything
    if not mr_found:
        try:
            gh_result = subprocess.run(
                ["gh", "pr", "view", branch_name, "--json",
                 "number,title,state,baseRefName,statusCheckRollup,body,"
                 "additions,deletions,changedFiles,headRefOid,mergeable"],
                capture_output=True, text=True, timeout=5, encoding="utf-8", errors="replace",
            )
            if gh_result.returncode == 0:
                pr = _json.loads(gh_result.stdout)
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

                # Extract linked issue
                body = pr.get("body") or ""
                issue_match = _re.search(r'(?:closes|fixes|resolves)?\s*#(\d+)', body, _re.IGNORECASE)
                if issue_match:
                    print(f"Issue: #{issue_match.group(1)}")
                mr_found = True
        except (FileNotFoundError, subprocess.TimeoutExpired, _json.JSONDecodeError):
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
