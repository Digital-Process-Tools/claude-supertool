#!/usr/bin/env python3
"""GitHub pull request details via gh CLI."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections import Counter
from typing import Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _body  # noqa: E402  (the one body cap + disclosure — #698)
import _untrusted  # noqa: E402  (the fence around tracker text — #694)
import _checks  # noqa: E402  (the one check tally, shared with gh-prs / git-status)
import _declared_legs  # noqa: E402  (the second leg count, shared with gh-run / gh-branch)
import _repo_target  # noqa: E402  (the repo this call is about, when not the cwd's)
import _branch_locale  # noqa: E402  (where the branch is checked out — shared by all five #850)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _pr_diff  # noqa: E402  (the review shape of a PR's diff — #875)

DESCRIPTION_MAX = 2000
COMMENT_MAX = 500
# `gh pr diff` streams a whole patch; the dashboard's 10s is sized for JSON
# metadata and an 80-file diff routinely outruns it.
DIFF_TIMEOUT = 60


def _relative_age(iso: str) -> str:
    """Format an ISO timestamp as 'Nd ago', 'Nh ago', or 'Nm ago'."""
    if not iso:
        return "?"
    try:
        from datetime import datetime, timezone
        s = iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        delta = datetime.now(timezone.utc) - dt
        secs = int(delta.total_seconds())
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"
    except (ValueError, ImportError):
        return "?"


def _gh(args: list[str], timeout: int = 10) -> subprocess.CompletedProcess[str]:
    """Run a gh command and return the result.

    A repo target (#673) becomes `--repo OWNER/NAME` on every subcommand that
    takes one. `gh api` does not take it — and does not need it: the GraphQL
    callers below read owner and repo off the PR's own URL and pass them as
    query variables, so they follow the target without being told.
    """
    if args and args[0] != "api":
        args = args + _repo_target.gh_args()
    return subprocess.run(
        ["gh"] + args,
        capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
    )


_RUN_ID_IN_URL = re.compile(r"/actions/runs/([0-9]+)(?:[/?#]|$)")

# How many distinct *workflows* one PR may be reconciled against before the op
# stops paying for it. The cost is one `gh api` call per workflow and `:status`
# is a hot path; a PR fanning out past this is outside what a single merge-gate
# call should spend, so the tally declines (UNVERIFIED) rather than either
# skipping the check or quietly blocking on N calls.
#
# Was 4, measured against the four workflows this repo had when #724 was
# written. Copilot code review added a fifth, and every PR carrying it rendered
# `TALLY UNVERIFIED` — a decline caused entirely by the op's own budget, worded
# as though something about the PR were unknown (#1181). 8 leaves room for a
# repo to grow a workflow without silently blinding its own merge gate; the
# per-workflow collapse below is what keeps the call count near the old one.
MAX_RECONCILED_RUNS = 8


def _rollup_run_ids(rollup: object) -> list[str]:
    """Distinct Actions run ids named by a rollup, in first-seen order.

    The id rides on `detailsUrl`, already fetched — the same field
    `_checks.github_job_id()` reads for the job id (#619), so this costs no
    extra request. Entries pointing at anything other than an Actions run
    (external CI, legacy commit statuses) contribute no id, which is what
    keeps them out of the reconciliation entirely.
    """
    if not isinstance(rollup, list):
        return []
    seen: list[str] = []
    for c in rollup:
        if not isinstance(c, dict):
            continue
        m = _RUN_ID_IN_URL.search(str(c.get("detailsUrl") or ""))
        if m and m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def _actions_leg_names(rollup: object) -> list[str]:
    """Names of the rollup entries that belong to an Actions run."""
    if not isinstance(rollup, list):
        return []
    out: list[str] = []
    for c in rollup:
        if not isinstance(c, dict):
            continue
        if _RUN_ID_IN_URL.search(str(c.get("detailsUrl") or "")):
            out.append(str(c.get("name") or c.get("context") or "?"))
    return out


def _missing_names(declared: Sequence[str], found: Sequence[str]) -> list[str]:
    """Declared leg names with the found ones removed, duplicates respected."""
    remaining = Counter(found)
    out: list[str] = []
    for name in declared:
        if remaining.get(name, 0):
            remaining[name] -= 1
        else:
            out.append(name)
    return out


def _runs_on_commit(owner: str, repo: str, sha: str) -> list | None:
    """`[(run_id, workflow_name, workflow_id)]` for the head commit, or `None`.

    The third element is the identity the collapse in `_one_run_per_workflow`
    keys on. A workflow's `name:` is not unique — two workflow files may spell
    it identically — and collapsing on the name would drop one of them from the
    declared count, which then reconciles silently on `declared <= found`. That
    is the shortfall this whole mechanism exists to catch, hidden by the fix
    for the noise it was making. `workflow_id` rides on the same response and
    costs nothing.

    **This is the fix for #804's comment, and it is one line of reasoning.**
    The declared count used to be summed over the run ids parsed out of the
    rollup — the very list it was checking. A run entirely absent from the
    rollup then contributes nothing to *either* side and cancels out, so the
    mechanism was structurally unable to see the case it was built for. On PR
    #822 that rendered `checks: 4 total: 4 passed` against an 18-leg matrix,
    with #724's reconciliation present, silent, and correct about the one run
    it could see.

    Runs are listed from the commit instead, so a run whose legs have not
    reached the rollup is still on the declared side. One extra request per
    render, on an op that sits in the merge gate — the trade #804 asks to be
    stated: `gh-pr:status` is the line a maintainer reads before merging, and
    a request is cheaper than a merge on four green CodeQL legs.
    """
    if not owner or not repo or not sha:
        return None
    try:
        r = _gh(["api", f"repos/{owner}/{repo}/actions/runs"
                        f"?head_sha={sha}&per_page=100"])
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    runs = data.get("workflow_runs") if isinstance(data, dict) else None
    if not isinstance(runs, list):
        return None
    out = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        rid = str(run.get("id") or "").strip()
        if rid:
            out.append((rid, str(run.get("name") or f"run #{rid}"),
                        str(run.get("workflow_id") or "").strip()))
    return out


def _one_run_per_workflow(ordered: list, keys_by_id: dict) -> list:
    """Collapse repeat run records of one workflow on one commit (#1181).

    `actions/runs?head_sha=` returns a record per run, and a re-run or a second
    trigger of the same workflow file adds another against the same sha: PRs
    #1177 and #1178 each carry **five** `changelog` records. They declare the
    same legs, so reconciling all five buys nothing and costs the whole
    budget — measured, both PRs tipped past the cap and rendered
    `TALLY UNVERIFIED` with nothing about them actually unknown.

    First seen wins. The rollup's own ids lead `ordered`, and GitHub lists runs
    newest-first, so the record kept is the one the rollup is showing.

    Keyed on `workflow_id`, never on the workflow's display name: `name:` is
    not unique across workflow files, and merging two real workflows would
    shrink `declared` into a silent reconcile.

    A run whose workflow id is unresolvable keys on its own run id rather than
    on the empty string: collapsing two unknowns into one would be this fix
    inventing the silence it exists to remove.
    """
    out: list = []
    seen: set[str] = set()
    for rid, name in ordered:
        key = keys_by_id.get(rid) or f"#{rid}"
        if key in seen:
            continue
        seen.add(key)
        out.append((rid, name))
    return out


def _declared_for_commit(d: dict) -> tuple:
    """`(declared, names, uncovered, reason)` — the second source, off the commit.

    `reason` is why `declared` is `None`, in words, and it is the point of
    #1181: "could not be established" is true of every cause and actionable for
    none, so a decline that fires for a whole afternoon reads exactly like the
    one that matters. Empty whenever `declared` is a number.

    **This is #804's comment, and the whole of it.** The declared count used to
    be summed over the run ids parsed out of the rollup — the very list it was
    checking. A run entirely absent from the rollup contributes nothing to
    *either* side and cancels out, so the mechanism was structurally unable to
    see the case it was built for. On PR #822 that rendered
    `checks: 4 total: 4 passed` against an 18-leg matrix, with #724's
    reconciliation present, silent, and correct about the one run it could see.

    `uncovered` names the runs on this commit that declare no leg at all. They
    are unreachable by arithmetic — zero on both sides reconciles — so they are
    reported in words. A run whose jobs GitHub has not created yet is the exact
    shape of the just-pushed window #822 was read in.

    `(None, [], [])` on every failure and never a fallback: falling back to the
    rollup's own ids restores the blind mechanism silently, which is worse than
    declining because it looks like an answer.
    """
    rollup = d.get("statusCheckRollup")
    rollup_ids = _rollup_run_ids(rollup)
    owner, repo = _declared_legs.owner_repo(d.get("url") or "")
    runs = _runs_on_commit(owner, repo, str(d.get("headRefOid") or ""))
    if runs is None:
        return (None, [], [], "the run list for this commit could not be read")

    ordered: list = [(rid, "") for rid in rollup_ids]
    known = set(rollup_ids)
    keys_by_id = {}
    for rid, name, workflow_id in runs:
        keys_by_id[rid] = workflow_id
        if rid not in known:
            ordered.append((rid, name))
            known.add(rid)
    ordered = _one_run_per_workflow(ordered, keys_by_id)
    if not ordered:
        return (0, [], [], "")
    if len(ordered) > MAX_RECONCILED_RUNS:
        return (None, [], [],
                f"{len(ordered)} distinct workflows on this commit exceed the "
                f"reconciliation cap of {MAX_RECONCILED_RUNS}")

    declared_names: list[str] = []
    uncovered: list[str] = []
    for rid, name in ordered:
        names = _declared_legs.legs_for_run(owner, repo, rid)
        if names is None:
            return (None, [], [],
                    f"the job list for run {name or rid} could not be read")
        declared_names.extend(names)
        if not names and rid not in rollup_ids:
            uncovered.append(name or f"run #{rid}")
    return (len(declared_names), declared_names, uncovered, "")


def _reconcile_checks(d: dict) -> tuple[str, list[str]]:
    """`(marker, lines)` disclosing legs the rollup never carried (#724/#804).

    Two independent gaps, because they are established two different ways and
    a reader deciding a merge needs both:

    * **the leg shortfall** — `shortfall()`'s arithmetic over every run on the
      commit, which catches a rollup short of runs whose jobs exist.
    * **the uncovered run** — a whole run contributing nothing, which the
      arithmetic cannot see. Stated in words, because an omitted field reads
      as "nothing to report", and that reading is the defect.

    Silent when nothing Actions-shaped is reachable at all: no legs read, no
    run declared, nothing to be short of. Printing a warning over a purely
    external check suite is noise where nothing is missing, and a marker that
    fires on every PR is one nobody reads.
    """
    found_names = _actions_leg_names(d.get("statusCheckRollup"))
    declared, declared_names, uncovered, reason = _declared_for_commit(d)
    if declared is None and not found_names and not uncovered:
        return ("", [])

    missing = _missing_names(declared_names, found_names)
    marker, lines = _checks.shortfall(len(found_names), declared, missing,
                                      reason=reason)
    if uncovered:
        shown = ", ".join(uncovered[:_checks.NAMED_CAP])
        if len(uncovered) > _checks.NAMED_CAP:
            shown += f", +{len(uncovered) - _checks.NAMED_CAP} more"
        one = len(uncovered) == 1
        lines = list(lines) + [
            f"  not covered: {shown} — {'that run' if one else 'those runs'} on "
            f"this commit ha{'s' if one else 've'} no job yet, so how many legs "
            f"{'it declares' if one else 'they declare'} is UNKNOWN and none of "
            "them are in this tally."
        ]
        marker = marker or _checks.INCOMPLETE_MARK
    return (marker, lines)

def _leg_unit_line(check_states: Sequence[str]) -> str:
    """Say that the failed count counts legs, when there is a failed count.

    #1050. `checks: 20 total: 16 passed, 4 failed, 0 pending` was read as four
    failing *tests*. There were six, uniform across four *legs*, and the wrong
    reading is not a careless one — nothing in the line names its unit, and the
    named disclosure under it lists check names that look exactly like test
    parametrisations (`pytest (windows-latest, 3.9)`).

    The two readings point at opposite investigations. Three visible names out
    of "four failures" says some legs passed where their twins failed, which is
    ordering or shared state; six-of-six on every leg says the fixture. The
    render was consistent with both and settled neither.

    Printed only when something is in the failed bucket. A unit note on a green
    PR is a line nobody needs, and a line that appears on every render is one
    nobody reads by the time it matters — this repo has paid for that twice.
    """
    if not any(_checks.bucket(s) == "failed" for s in check_states):
        return ""
    return ("  (those are LEGS — one check run each, not one test each. For the "
            "test counts read a leg's own summary: ./supertool 'gh-job:ID')")


def _local_branch_check(source: str) -> str:
    """Return a one-line local-branch-vs-PR-source check.

    Delegated to `_branch_locale` (#850): a branch held by a linked worktree is
    neither a match nor a MISMATCH, and saying MISMATCH there prescribed a
    checkout git refuses.
    """
    return _branch_locale.check(source)


def _fetch_review_threads(url: str, number: int | str) -> list[dict]:
    """Fetch reviewThreads via GraphQL — gh pr view --json doesn't expose them.

    Parses owner/repo from PR URL. Returns [] on any failure (silent fallback).
    """
    if not url:
        return []
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/pull/\d+", url)
    if not m:
        return []
    owner, repo = m.group(1), m.group(2)
    query = (
        "query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r)"
        "{pullRequest(number:$n){reviewThreads(first:100){nodes{isResolved}}}}}"
    )
    try:
        r = _gh([
            "api", "graphql",
            "-f", f"query={query}",
            "-F", f"o={owner}", "-F", f"r={repo}", "-F", f"n={number}",
        ])
        if r.returncode != 0:
            return []
        data = json.loads(r.stdout)
        repo_node = (data.get("data") or {}).get("repository") or {}
        pr_node = repo_node.get("pullRequest") or {}
        threads = pr_node.get("reviewThreads") or {}
        return threads.get("nodes") or []
    except (subprocess.TimeoutExpired, json.JSONDecodeError, AttributeError, TypeError):
        return []


def _head_commit_age_secs(url: str, number: int | str) -> int | None:
    """Seconds since the PR's head commit was made, or None if unestablished.

    Costs one GraphQL call, so it is only ever asked when a commit carries zero
    check runs — the tally needs no help when runs exist, and `gh-pr` is a hot
    path.

    GraphQL exposes `pushedDate`, which is the field this wants and which
    GitHub now returns as `null` for every commit (verified against this repo's
    own PRs), so the age comes from `committedDate`. That can predate the push
    — a commit can sit locally for a day — which only ever makes the age look
    *older* than the wait actually was. `_checks.absence()` treats old-and-empty
    on an open PR as UNKNOWN rather than as proof, so the skew cannot
    manufacture a "no runs will be created"; the worst it does is turn a
    freshly pushed old commit's "not yet" into "go look", which is safe.

    None on every failure — no repo in the URL, gh error, missing node. The
    caller must render that as a decline, not as either verdict.
    """
    if not url:
        return None
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/pull/\d+", url)
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    query = (
        "query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r)"
        "{pullRequest(number:$n){commits(last:1){nodes{commit"
        "{pushedDate committedDate}}}}}}"
    )
    try:
        r = _gh([
            "api", "graphql",
            "-f", f"query={query}",
            "-F", f"o={owner}", "-F", f"r={repo}", "-F", f"n={number}",
        ])
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout)
        repo_node = (data.get("data") or {}).get("repository") or {}
        pr_node = repo_node.get("pullRequest") or {}
        nodes = (pr_node.get("commits") or {}).get("nodes") or []
        if not nodes:
            return None
        commit = nodes[-1].get("commit") or {}
        stamp = commit.get("pushedDate") or commit.get("committedDate") or ""
        if not stamp:
            return None
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        return int((datetime.now(timezone.utc) - dt).total_seconds())
    except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError,
            AttributeError, TypeError, ImportError):
        return None


def _absence_lines(d: dict, number: int | str) -> tuple[str, str]:
    """`(checks_text, mergeable_note)` for a head commit with zero check runs."""
    return _checks.absence(
        d.get("state"),
        _head_commit_age_secs(d.get("url") or "", number),
        mergeable=d.get("mergeable"),
    )


def _format_error(stderr: str, resource: str, identifier: str) -> str:
    """Classify gh errors into actionable messages for LLMs."""
    s = stderr.lower()
    if "github host" in s or "not a git repository" in s or "git remotes" in s:
        return _repo_target.no_repo_error("gh-pr:265:status")
    if "could not resolve" in s or "404" in s or "not found" in s:
        return (f"ERROR: {resource} #{identifier} not found "
                f"{_repo_target.not_found_scope()}. "
                f"{_repo_target.not_found_hint()}")
    if "401" in s or "unauthorized" in s or "not logged in" in s or "token" in s:
        return f"ERROR: gh CLI not authenticated. Run: gh auth login (verify with: gh auth status)"
    if "rate limit" in s or "429" in s:
        return "ERROR: GitHub API rate limit exceeded. Wait a few minutes and retry."
    if "403" in s or "forbidden" in s:
        return f"ERROR: permission denied for {resource} #{identifier}. Check repo access (gh auth status)."
    return f"ERROR: gh failed for {resource} #{identifier}: {stderr.strip()}"


def _diff_header(number: str) -> list[str]:
    """The two lines a diff needs for context, and never a reason to fail.

    The diff is the load-bearing read; the title and branch pair are context.
    So a metadata call that does not come back degrades those to `?` rather
    than aborting — blocking the review read on the decorative one would be
    this repo's defect class wearing a helpful face.
    """
    head = [_untrusted.banner()]
    try:
        meta = _gh(["pr", "view", number, "--json",
                    "number,title,headRefName,baseRefName,url"])
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        head.append(f"# PR #{number} (title unavailable: {exc})")
        return head
    if meta.returncode != 0:
        head.append(f"# PR #{number} (title unavailable: "
                    f"{(meta.stderr or '').strip()[:80] or 'gh pr view failed'})")
        return head
    try:
        d = json.loads(meta.stdout)
    except json.JSONDecodeError:
        head.append(f"# PR #{number} (title unavailable: unparseable JSON)")
        return head
    head.append(f"# PR #{d.get('number', number)} "
                f"{_untrusted.flat(str(d.get('title') or '?'))}")
    head.append(f"Branch: {_untrusted.flat(str(d.get('headRefName') or '?'))} "
                f"-> {_untrusted.flat(str(d.get('baseRefName') or '?'))}")
    if d.get("url"):
        head.append(f"URL: {d['url']}")
    return head


def _run_diff(number: str, path: str | None) -> int:
    """`gh-pr:N:diff[:PATH]` — the merge gate's read, in a reviewable shape.

    `gh pr diff` carries `--repo` through `_gh`, so a call made from the wrong
    directory answers about the repo the caller named rather than about
    whatever the cwd's remote happens to be (#677/#678).

    **No `--patch` (#1068).** `--patch` is format-patch: one section per
    commit, so a file touched by three commits arrives three times and the
    hunks route served the first and stopped. The bare `gh pr diff` is the net
    three-dot diff — merge-base to head, one entry per path — which is the
    thing being merged and therefore the thing under review. GitHub computes
    it, so nothing here reassembles anything. A per-commit view is a different
    question ("what changed since I last looked") and needs a since-ref rather
    than a flag; it is not this op.

    Every failure route hands `_pr_diff.render` a `None` file list with the
    cause attached. An exception, a non-zero exit and an unreadable patch are
    three different reasons and none of them is "this PR changes nothing".
    """
    header = _diff_header(number)
    files: list[dict] | None
    reason: str | None = None
    try:
        result = _gh(["pr", "diff", number], timeout=DIFF_TIMEOUT)
    except subprocess.TimeoutExpired:
        files, reason = None, f"gh pr diff timed out after {DIFF_TIMEOUT}s"
    except (FileNotFoundError, OSError) as exc:
        files, reason = None, f"gh pr diff could not run: {exc}"
    else:
        if result.returncode != 0:
            files = None
            reason = (f"gh pr diff exited {result.returncode}: "
                      f"{(result.stderr or '').strip()[:200] or 'no stderr'}")
        else:
            files = _pr_diff.parse(result.stdout)
    text, code = _pr_diff.render(files, header=header, path=path,
                                 reason=reason, number=str(number))
    print(text)
    return code


def main() -> int:
    if len(sys.argv) < 2:
        print("ERROR: usage: pr.py NUMBER_OR_BRANCH [status|full|diff[:PATH]]")
        return 1

    arg = sys.argv[1]
    flags = sys.argv[2:]
    slim = "status" in flags
    # gh-pr had no :full at all. The truncation disclosure names one as the way
    # to get the withheld text, so one has to exist — a stated escape hatch
    # that does not work is worse than none, because it stops the reader
    # looking for another (#698).
    full = "full" in flags
    desc_max = None if full else DESCRIPTION_MAX
    comment_max = None if full else COMMENT_MAX

    # If not all digits, treat as branch name
    if not arg.isdigit():
        try:
            branch_result = _gh([
                "pr", "list", "--head", arg, "--json", "number",
                "--limit", "1"
            ])
            if branch_result.returncode == 0:
                prs = json.loads(branch_result.stdout)
                if prs:
                    arg = str(prs[0].get("number", arg))
                else:
                    # Try closed PRs too
                    branch_result2 = _gh([
                        "pr", "list", "--head", arg, "--state", "all",
                        "--json", "number", "--limit", "1"
                    ])
                    if branch_result2.returncode == 0:
                        prs2 = json.loads(branch_result2.stdout)
                        if prs2:
                            arg = str(prs2[0].get("number", arg))
                        else:
                            print(f"ERROR: no PR found for branch {arg!r}")
                            return 1
        except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            print(f"ERROR: branch lookup failed: {e}")
            return 1

    # The diff route runs before the dashboard fetch: it needs none of those
    # fields and the dashboard call is ~20x the payload.
    if "diff" in flags:
        rest = flags[flags.index("diff") + 1:]
        return _run_diff(arg, rest[0] if rest else None)

    # Fetch PR with all needed fields
    try:
        result = _gh([
            "pr", "view", arg, "--json",
            "number,title,state,author,headRefName,baseRefName,labels,"
            "milestone,reviewDecision,reviews,mergeCommit,mergeable,"
            "isDraft,url,body,comments,additions,deletions,changedFiles,"
            "statusCheckRollup,assignees,createdAt,updatedAt,headRefOid"
        ])
    except FileNotFoundError:
        print("ERROR: gh not found — install from https://cli.github.com")
        return 1
    except subprocess.TimeoutExpired:
        print("ERROR: gh timed out")
        return 1

    if result.returncode != 0:
        print(_format_error(result.stderr, "PR", arg))
        return 1

    try:
        d = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"ERROR: invalid JSON from gh\n{result.stdout[:500]}")
        return 1

    if slim:
        iid = d.get("number", arg)
        state = d.get("state", "?")
        mergeable = d.get("mergeable", "?")
        review_decision = d.get("reviewDecision") or "none"
        check_states = _checks.github_states(d.get("statusCheckRollup"))
        merge_commit = (d.get("mergeCommit") or {}).get("oid", "")
        web_url = d.get("url", "")
        conflicts = "yes" if mergeable == "CONFLICTING" else "no"
        print(f"#{iid} | state: {state} | mergeable: {mergeable} | conflicts: {conflicts}")
        print(f"branch: {_untrusted.flat(d.get('headRefName') or '?')} -> "
              f"{_untrusted.flat(d.get('baseRefName') or '?')}")
        shortfall_lines: list[str] = []
        if check_states:
            # `with_age` is #801: a pending count with no age reads the same
            # whether the legs are queued and progressing or wedged, and this
            # is the line read on a poll loop.
            checks_text = _checks.summarize_github(
                d.get("statusCheckRollup"), with_age=True)
            marker, shortfall_lines = _reconcile_checks(d)
            if marker:
                checks_text += f" {marker}"
        else:
            checks_text, _ = _absence_lines(d, iid)
        print(f"checks: {checks_text}")
        for line in _checks.github_pending_lines(d.get("statusCheckRollup")):
            print(line)
        for line in shortfall_lines:
            print(line)
        for line in _checks.named_disclosure(
            _checks.github_named_states(d.get("statusCheckRollup"))
        ):
            print(line)
        unit_line = _leg_unit_line(check_states)
        if unit_line:
            print(unit_line)
        print(f"review: {review_decision}")
        if merge_commit:
            print(f"merge_commit: {merge_commit[:12]}")
        if web_url:
            print(f"url: {web_url}")
        return 0

    # One-line fields are flattened rather than fenced — see presets/_untrusted.py.
    title = _untrusted.flat(d.get("title", "?"))
    state = d.get("state", "?")
    iid = d.get("number", arg)
    source = _untrusted.flat(d.get("headRefName", "?"))
    target = _untrusted.flat(d.get("baseRefName", "?"))
    author = _untrusted.flat((d.get("author") or {}).get("login", "?"))
    web_url = d.get("url", "")
    labels = _untrusted.flat(", ".join(l.get("name", "?") for l in d.get("labels", [])) or "none")
    milestone = _untrusted.flat((d.get("milestone") or {}).get("title", "none"))
    draft = d.get("isDraft", False)
    mergeable = d.get("mergeable", "?")
    review_decision = d.get("reviewDecision") or "none"
    merge_commit = (d.get("mergeCommit") or {}).get("oid", "")
    additions = d.get("additions", "?")
    deletions = d.get("deletions", "?")
    changed_files = d.get("changedFiles", "?")

    body = d.get("body") or ""
    body_total = len(body)
    body, body_withheld = _body.cut(body, desc_max)

    # Header. The fence convention is declared before the first thing inside a
    # fence — the reader this protects is the one who acts on the first line.
    draft_marker = " [DRAFT]" if draft else ""
    print(_untrusted.banner())
    print(f"# #{iid} {title}{draft_marker}")
    print(f"State: {state} | Author: {author}")
    print(f"Branch: {source} -> {target}")
    local_check = _local_branch_check(source)
    if local_check:
        print(local_check)
    print(f"Labels: {labels}")
    print(f"Milestone: {milestone}")
    if body_withheld:
        # In the header, before ## Description — a footer-only notice is read
        # by nobody in exactly the case it exists for (#681, #698).
        print(_body.header_notice(body, body_total, body_withheld))

    # Assignees (distinct from reviewers)
    assignees = d.get("assignees") or []
    assignee_names = [a.get("login", "?") for a in assignees]
    print(f"Assignees: {', '.join(assignee_names) if assignee_names else 'none'}")

    # Age — created/updated, for stale-PR signal
    created_at = d.get("createdAt") or ""
    updated_at = d.get("updatedAt") or ""
    if created_at:
        age_str = f"Created: {_relative_age(created_at)}"
        if updated_at and updated_at != created_at:
            age_str += f" | Updated: {_relative_age(updated_at)}"
        print(age_str)

    # Unresolved review threads — fetched via GraphQL (not exposed by gh pr view --json)
    review_threads = _fetch_review_threads(d.get("url", ""), iid)
    if review_threads:
        unresolved = sum(1 for t in review_threads if not t.get("isResolved"))
        print(f"Unresolved threads: {unresolved} / {len(review_threads)}")

    # Reviews — always print so absence is signal, not silence
    reviews = d.get("reviews", [])
    if reviews:
        reviewers = {}
        for r in reviews:
            login = _untrusted.flat((r.get("author") or {}).get("login", "?"))
            r_state = r.get("state", "?")
            reviewers[login] = r_state  # latest review state per reviewer
        parts = [f"{login} ({state})" for login, state in reviewers.items()]
        print(f"Reviews: {', '.join(parts)}")
    else:
        print("Reviews: none")
    print(f"Review decision: {review_decision}")

    # Checks (CI status) — the tally accounts for every entry it was handed;
    # see presets/_checks.py for why the sum matters more than the labels.
    # An absent tally is two opposite readings, so the zero case is classified
    # rather than named — see _checks.absence() (#585). It buys the evidence for
    # that (one GraphQL call) only here, never when runs exist.
    check_states = _checks.github_states(d.get("statusCheckRollup"))
    shortfall_lines: list[str] = []
    if check_states:
        # `with_age` is #801 — see the `:status` branch above.
        checks_text = _checks.summarize_github(
            d.get("statusCheckRollup"), with_age=True)
        merge_note = "" if _checks.all_green(check_states) else (
            f" — checks {_checks.NOT_GREEN}, see Checks above"
        )
        # A tally that does not cover every leg is not a merge signal even when
        # every leg it *does* cover passed, so the caveat printed next to
        # `Mergeable:` has to carry it too — that is the line a reader stops
        # at when the answer looks green (#724).
        marker, shortfall_lines = _reconcile_checks(d)
        if marker:
            checks_text += f" {marker}"
            if not merge_note:
                merge_note = f" — checks {marker}, see Checks above"
    else:
        checks_text, merge_note = _absence_lines(d, iid)
    print(f"Checks: {checks_text}")
    for line in _checks.github_pending_lines(d.get("statusCheckRollup")):
        print(line)
    for line in shortfall_lines:
        print(line)
    for line in _checks.named_disclosure(
        _checks.github_named_states(d.get("statusCheckRollup"))
    ):
        print(line)
    unit_line = _leg_unit_line(check_states)
    if unit_line:
        print(unit_line)

    # Changes
    print(f"Changes: {changed_files} files, +{additions} -{deletions}")

    # Mergeable — GitHub's *merge conflict* state, not a CI verdict. Printed
    # bare underneath a check tally it reads as one, which is half of what made
    # #454 dangerous, so it names what it measures and carries the CI caveat.
    # merge_note was computed with the Checks line above: "unknown because
    # nothing has run yet" and "unknown because nothing will run" are different
    # answers to a merge question, and this printed one sentence for both.
    if mergeable == "CONFLICTING":
        print(f"Conflicts: YES — cannot merge{merge_note}")
    elif mergeable == "MERGEABLE":
        print(f"Mergeable: yes (no merge conflicts){merge_note}")
    else:
        print(f"Mergeable: {mergeable}{merge_note}")

    # Merge commit
    if merge_commit:
        print(f"Merge commit: {merge_commit[:12]}")

    if web_url:
        print(f"URL: {web_url}")

    # Linked issue — every issue a GitHub closing keyword actually binds to a
    # number, not the first `#N` in the body (#591). The pattern this replaces
    # made the keyword optional, and it lived here *and* in `git-status`
    # character-for-character; both now go through the one extractor.
    issue_refs = _checks.closing_issue_refs(d.get("body"))
    if not issue_refs:
        print(f"\n{_checks.linked_issue_line(issue_refs)}")
    for ref in issue_refs:
        # A cross-repo reference is printed as written and never fetched:
        # `gh issue view 5` resolves 5 against *this* repository, so fetching it
        # would print a different issue's title under this PR's closing
        # reference — #591's defect with more confidence attached.
        if not ref.startswith("#"):
            print(f"\nIssue: {ref} — in another repository, not fetched")
            continue
        issue_num = ref[1:]
        try:
            issue_result = _gh([
                "issue", "view", issue_num, "--json",
                "number,title,state,labels,assignees"
            ])
            if issue_result.returncode == 0:
                issue_data = json.loads(issue_result.stdout)
                i_title = issue_data.get("title", "?")
                i_state = issue_data.get("state", "?")
                i_labels = ", ".join(l.get("name", "") for l in issue_data.get("labels", []))
                i_assignees = ", ".join(a.get("login", "") for a in issue_data.get("assignees", []))
                print(f"\n## Issue #{issue_num} — {i_title}")
                info = f"State: {i_state}"
                if i_labels:
                    info += f" | Labels: {i_labels}"
                if i_assignees:
                    info += f" | Assignees: {i_assignees}"
                print(info)
        except (subprocess.TimeoutExpired, json.JSONDecodeError):
            print(f"\nIssue: {ref}")

    # Description
    if body:
        print(f"\n## Description\n{_untrusted.fence(body)}")
        if body_withheld:
            print(f"\n{_body.cut_notice(body_withheld)}")
    else:
        print("\n## Description\n_(empty)_")

    # Comments — the header printed the total and then showed the last ten of
    # them, with nothing in between saying so (#719).
    comments = d.get("comments", [])
    shown = comments if full else comments[-_body.COMMENT_TAIL:]
    print(f"\n{_body.comments_heading(len(shown), len(comments))}")
    for c in shown:
        c_author = _untrusted.flat((c.get("author") or {}).get("login", "?"))
        c_body = c.get("body") or ""
        # The truncation notice is supertool's, so it prints outside the fence
        # — see the same call in gh-issue.
        c_trunc = ""
        if comment_max is not None and len(c_body) > comment_max:
            c_body = c_body[:comment_max]
            c_trunc = _body.comment_cut_notice(comment_max)
        c_created = (c.get("createdAt") or "")[:10]
        print(f"\n**{c_author}** ({c_created}):")
        print(_untrusted.fence(c_body))
        if c_trunc:
            print(c_trunc)

    return 0


if __name__ == "__main__":
    sys.exit(main())
