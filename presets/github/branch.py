#!/usr/bin/env python3
"""Is this branch green? — answered per workflow, at a named commit (#615).

`gh-pr:N` answers for a pull request. After a squash merge the ref that matters
is the default branch, which has no PR — `gh-pr:master:status` returns *no PR
found for branch 'master'* — so the post-merge check was hand-rolled every time:

    gh run list --branch master --limit 1

**That line cannot answer the question it is asked.** A repo with more than one
`push` workflow has several runs per commit, and `--limit 1` returns whichever
*workflow* started most recently. On 2026-08-05 that was CodeQL, `completed
success`, while the `tests` matrix on the same SHA was still `queued`. The
statement "master is green" was true that morning and its method could not have
told anyone if it hadn't been.

So this op selects runs by **workflow identity**, never by recency, and its
summary is **conjunctive**: green only when every workflow that ran on the head
commit concluded and every leg of every one of them passed. One workflow's
conclusion is never allowed to stand in for the commit's.

Four states, because collapsing any pair of them is this repository's house
defect (`docs/validators.md` §"Declining instead of guessing"):

* ``GREEN`` — every workflow on the head SHA concluded, every leg ``SUCCESS``.
* ``NOT GREEN`` — a finding. Something failed, or something has not finished.
  Both are findings and they are worded differently, because "a leg failed" and
  "a leg has not started" are opposite next actions.
* ``NO RUN`` — nothing exists for this SHA, *with the reason*. Zero renders
  exactly like "not yet" and has been read that way; the grace window
  (`_checks.CHECK_CREATION_GRACE_SECS`, measured in #585) is what separates the
  two, and past it the cause is declined rather than guessed.
* ``UNKNOWN`` — something could not be read. An unread job list is never
  counted as zero passing legs.

The leg arithmetic is `presets/_checks.summarize`, the same module `gh-pr` and
`gh-run` render through — deliberately, because #615's own argument for the op
existing is that reusing one renderer keeps one place where a `CANCELLED` can be
mis-tallied. What is *not* reused is the render: `gh-pr` is handed a flat rollup
on one commit, and this is a set of runs grouped by workflow with a conjunction
over them, which is a different shape and would have arrived as a second data
model inside one function.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _checks  # noqa: E402
import _declared_legs  # noqa: E402  (the second leg count, shared with gh-run / gh-pr)
import _repo_target  # noqa: E402  (the repo this call is about, when not the cwd's)

# The four states. Spelled as constants because the tests, the exit code and
# the header all have to mean the same thing by them, and because `NOT GREEN`
# contains `GREEN` — anything comparing these as substrings cannot tell a
# verdict from its negation.
GREEN = "GREEN"
NOT_GREEN = "NOT GREEN"
NO_RUN = "NO RUN"
UNKNOWN = "UNKNOWN"

# A run's lifecycle phase, in this module's own words. #615 comment 1 is a
# worked case of a bare column (`[time]`) being read as a possible `TIMED_OUT`
# and costing a second call to disambiguate, so every row states its phase in a
# word that cannot be mistaken for a leg state.
PHASE_CONCLUDED = "concluded"
PHASE_RUNNING = "running"
PHASE_UNESTABLISHED = "unestablished"

# GitHub never transitions a run out of `completed` — same reading as
# `gh-run`'s `_TERMINAL_RUN_STATUS`, and right for the same reason: this is a
# run-*lifecycle* field, which has one terminal value GitHub owns, unlike the
# set of leg states, which grows.
_TERMINAL_RUN_STATUS = "completed"

# How far back the run list reaches. Enough to cover the head commit *and* the
# one before it — the previous head is what makes "this workflow ran last time
# and not this time" observable at all — with room for a repo running several
# workflows per push. Not a page of history: nothing older than the previous
# head is read.
RUN_LIST_LIMIT = 60

# Job lists are fetched one call per workflow, in parallel. Small on purpose:
# the realistic workflow count per SHA is two or three, and a wide pool would
# only buy latency on a repo that does not exist yet.
JOB_WORKERS = 4

_GRACE = _checks.CHECK_CREATION_GRACE_SECS


# ---------------------------------------------------------------------------
# selection — by workflow identity, never by recency
# ---------------------------------------------------------------------------

def latest_per_workflow(runs: object, sha: str) -> dict:
    """The newest run of each workflow **on this SHA**, keyed by workflow name.

    Two filters, and both are load-bearing.

    *By SHA*, because "the branch is green" is a claim about a commit, and a
    run on the previous head is a true statement about a commit the reader has
    already moved past.

    *By workflow*, because that is the axis `--limit 1` collapses. Keeping the
    newest run per workflow — highest `databaseId`, which GitHub allocates
    monotonically — means a re-run supersedes the run it replaces, while a
    *different* workflow never supersedes anything.
    """
    if not isinstance(runs, list) or not sha:
        return {}
    out: dict = {}
    for r in runs:
        if not isinstance(r, dict) or r.get("headSha") != sha:
            continue
        name = str(r.get("workflowName") or "?")
        prev = out.get(name)
        if prev is None or _run_id(r) >= _run_id(prev):
            out[name] = r
    return out


def _run_id(run: object) -> int:
    try:
        return int(run.get("databaseId"))  # type: ignore[union-attr]
    except (AttributeError, TypeError, ValueError):
        return -1


def previous_head(runs: object, sha: str) -> tuple[str, set]:
    """`(sha, workflow names)` of the newest run set that is *not* this SHA.

    The only evidence available for "a workflow that normally runs here did not
    run on this commit". It is evidence, not a rule: a path filter makes the
    absence legitimate, so nothing here ever concludes on its own — see
    `verdict()`, which only lets it block green inside the creation window.
    """
    if not isinstance(runs, list):
        return "", set()
    prev = ""
    for r in runs:
        if not isinstance(r, dict):
            continue
        s = str(r.get("headSha") or "")
        if s and s != sha:
            prev = s
            break
    if not prev:
        return "", set()
    names = {str(r.get("workflowName") or "?") for r in runs
             if isinstance(r, dict) and r.get("headSha") == prev}
    return prev, names


def run_phase(run: object) -> str:
    """`concluded` | `running` | `unestablished` for one run.

    A concluded run and one still moving must be unmistakably distinct (#615
    comment 1), and an unreadable lifecycle field is a third thing again — it
    is not evidence that the run is still going.
    """
    if not isinstance(run, dict):
        return PHASE_UNESTABLISHED
    raw = str(run.get("status") or "").strip().lower()
    if not raw:
        return PHASE_UNESTABLISHED
    return PHASE_CONCLUDED if raw == _TERMINAL_RUN_STATUS else PHASE_RUNNING


def leg_summary(states) -> str:
    """The shared tally, not a second derivation of it.

    Exists as a named seam so a test can assert this op and `_checks` agree by
    identity rather than by two renderings that happen to match today.
    """
    return _checks.summarize(states)


# ---------------------------------------------------------------------------
# the conjunction
# ---------------------------------------------------------------------------

def _duration(secs: object) -> str:
    if secs is None:
        return "age unestablished"
    n = max(0, int(secs))
    if n < 60:
        return f"{n}s"
    if n < 3600:
        return f"{n // 60}m"
    if n < 86400:
        return f"{n // 3600}h"
    return f"{n // 86400}d"


def _window(grace: int) -> str:
    return f"~{max(1, grace // 60)}min"


def no_run_verdict(sha: str, age_secs: object, grace: int = _GRACE) -> tuple:
    """Zero runs on this SHA, rendered as *why* rather than as a zero.

    Three readings, and none of them is green. The window is the one measured
    in #585 for GitHub's own run-creation latency; past it the cause is
    declined, because a workflow can legitimately not fire for a ref (path
    filters) and inferring that from `.github/workflows/*` would be inferring
    it from files that need not be the ones on this ref.
    """
    short = sha[:7] if sha else "an unestablished commit"
    if age_secs is None:
        return (NO_RUN, f"{NO_RUN} — zero workflow runs on {short}, and when "
                        "the head commit landed could not be established, so "
                        "whether one is still coming is UNKNOWN. Nothing has "
                        "passed. Check the repo's Actions tab.")
    if int(age_secs) <= grace:
        return (NO_RUN, f"{NO_RUN} — zero workflow runs on {short}; the head "
                        f"commit is {_duration(age_secs)} old, inside the "
                        f"{_window(grace)} window in which a first run has "
                        "always appeared, so a run is still expected. Nothing "
                        "has passed and nothing has failed.")
    return (NO_RUN, f"{NO_RUN} — zero workflow runs on {short}, head commit "
                    f"{_duration(age_secs)} old and past the {_window(grace)} "
                    "window in which a first run normally appears. Whether any "
                    "workflow covers this ref is UNKNOWN — a path filter and a "
                    "workflow that never fired look identical from here. "
                    "Check the repo's Actions tab.")


def verdict(selected: dict, legs: dict, missing, sha: str,
            age_secs: object, grace: int = _GRACE,
            unreconciled: str = "") -> tuple:
    """`(state, sentence)` for the whole commit. Conjunctive, and ordered.

    `legs` maps a workflow name to its leg states, or to ``None`` when the job
    list did not come back. ``None`` is not zero: a workflow whose legs were
    never read cannot contribute to a green, so it decides the whole answer.

    The order the findings are tested in is the order a reader acts on them —
    unread beats failed beats unfinished beats not-yet-created — and every
    branch names what it is talking about, because a verdict that says "not
    green" without naming the workflow sends the reader back to the web UI,
    which is the cost this op exists to remove.

    `unreconciled` is `_checks.shortfall`'s marker when the legs read could not
    be squared with the legs the runs declare (#837). It is tested last, and
    only against the green: every branch above it is a finding about the legs
    that *were* read, and a finding beats a doubt. But a green is a claim about
    all of them, and "every leg I managed to read passed" is not that claim —
    on a merge gate the difference is the whole point of the op.
    """
    if not selected:
        return no_run_verdict(sha, age_secs, grace)

    short = sha[:7] if sha else "?"

    unread = sorted(n for n, v in legs.items() if v is None)
    if unread:
        return (UNKNOWN, f"{UNKNOWN} — the job list for {_names(unread)} did "
                         f"not come back, so how many legs ran on {short} is "
                         "UNKNOWN and nothing here establishes green. Re-run "
                         "the op; if it persists, count by hand with "
                         "`gh run view <run-id> --json jobs`.")

    red_wfs = sorted(n for n, states in legs.items()
                     if any(_checks.is_red(s) for s in (states or []))
                     or _checks.is_red(_run_conclusion(selected[n])))
    if red_wfs:
        bad = sum(1 for states in legs.values()
                  for s in (states or []) if _checks.is_red(s))
        legword = "leg" if bad == 1 else "legs"
        return (NOT_GREEN, f"{NOT_GREEN} — {bad} {legword} on {short} did not "
                           f"pass, in {_names(red_wfs)}. Named below.")

    moving = sorted(n for n, r in selected.items()
                    if run_phase(r) != PHASE_CONCLUDED
                    or any(_checks.bucket(s) == "pending"
                           for s in (legs.get(n) or [])))
    if moving:
        return (NOT_GREEN, f"{NOT_GREEN} — nothing has failed, but "
                           f"{_names(moving)} has not concluded on {short}, so "
                           f"{'it is' if len(moving) == 1 else 'they are'} "
                           "neither a pass nor a fail. The commit is not "
                           "cleared.")

    if missing and age_secs is not None and int(age_secs) <= grace:
        return (NOT_GREEN, f"{NOT_GREEN} — {_names(sorted(missing))} ran on the "
                           f"previous head and has no run on {short}; the head "
                           f"commit is {_duration(age_secs)} old, inside the "
                           f"{_window(grace)} creation window, so a run is "
                           "still expected. Waiting is the correct action.")

    n_legs = sum(len(v or []) for v in legs.values())
    n_wf = len(selected)
    if unreconciled:
        return (UNKNOWN, f"{UNKNOWN} — every one of the {n_legs} legs read on "
                         f"{short} passed, but the tally could not be squared "
                         f"with what the runs declare ({unreconciled}), so "
                         "whether these are all of the legs is UNKNOWN. "
                         "Detailed below. Nothing here has failed.")
    return (GREEN, f"{GREEN} — every workflow on {short} concluded and every "
                   f"leg passed ({n_legs} legs across {n_wf} "
                   f"{'workflow' if n_wf == 1 else 'workflows'}).")


def _run_conclusion(run: object) -> str:
    if not isinstance(run, dict):
        return _checks.UNKNOWN
    raw = str(run.get("conclusion") or "").strip()
    if raw:
        return _checks.normalize(raw)
    # Not concluded yet — that is the `moving` branch's business, not a red.
    return "PENDING"


def _names(names) -> str:
    items = list(names)
    if len(items) == 1:
        return f"`{items[0]}`"
    return ", ".join(f"`{n}`" for n in items)


# ---------------------------------------------------------------------------
# gh plumbing
# ---------------------------------------------------------------------------

def _gh(argv: list, timeout: int = 20):
    return subprocess.run(argv, capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace")


def _format_error(stderr: str, what: str) -> str:
    s = (stderr or "").lower()
    if "github host" in s or "not a git repository" in s or "git remotes" in s:
        return _repo_target.no_repo_error("gh-branch:master")
    # 422 + "no commit found for sha" is what the commits endpoint returns for
    # a ref that does not exist — not a 404. Left unclassified it echoed gh's
    # own sentence, which names a SHA for something the caller typed as a
    # branch name and reads as an API fault rather than a typo.
    if ("could not resolve" in s or "404" in s or "not found" in s
            or "422" in s or "no commit found" in s):
        # Scope is shared; the hint is not. `_repo_target.not_found_hint()`
        # says "Check the number", which is right for the issue/PR ops it was
        # written for and wrong for a ref the caller typed as a name.
        target = _repo_target.target()
        where = f" (gh repo view {target})" if target else ""
        return (f"ERROR: {what} not found {_repo_target.not_found_scope()}. "
                f"Check the spelling, or that the branch is pushed"
                f"{where}.")
    if "401" in s or "unauthorized" in s or "not logged in" in s:
        return "ERROR: gh CLI not authenticated. Run: gh auth login"
    if "rate limit" in s or "429" in s:
        return "ERROR: GitHub API rate limit exceeded. Wait a few minutes."
    if "403" in s or "forbidden" in s:
        return f"ERROR: permission denied for {what}. Check repo access."
    return f"ERROR: gh failed for {what}: {(stderr or '').strip()}"


def _repo_identity():
    """`(nameWithOwner, defaultBranchRef, error)`.

    Read on every call, not only the no-argument one: the head SHA and the
    verdict are claims about a specific repository, and a call carrying a
    `repo:` target reads as being about the cwd's repo unless it says otherwise.
    """
    target = _repo_target.target()
    argv = ["gh", "repo", "view"] + ([target] if target else []) + \
        ["--json", "nameWithOwner,defaultBranchRef"]
    try:
        r = _gh(argv)
    except FileNotFoundError:
        return "", "", "ERROR: gh not found — install from https://cli.github.com"
    except subprocess.TimeoutExpired:
        return "", "", "ERROR: gh timed out resolving the repository"
    if r.returncode != 0:
        return "", "", _format_error(r.stderr, "this repository")
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        return "", "", "ERROR: invalid JSON from gh repo view"
    ref = d.get("defaultBranchRef") or {}
    return (str(d.get("nameWithOwner") or "?"),
            str(ref.get("name") or ""), "")


def _head_commit(ref: str):
    """`(sha, age_secs, error)` for the branch's head commit.

    Resolved from the *ref*, never from the run list. Deriving the SHA from the
    newest run would make "no run exists for this commit" — the state #615's
    second comment is most concerned with — structurally unreachable: the
    answer would always be the SHA of a commit that had a run.
    """
    try:
        r = _gh(["gh", "api", _repo_target.api_path(f"commits/{ref}")])
    except FileNotFoundError:
        return "", None, "ERROR: gh not found — install from https://cli.github.com"
    except subprocess.TimeoutExpired:
        return "", None, f"ERROR: gh timed out resolving branch {ref!r}"
    if r.returncode != 0:
        return "", None, _format_error(r.stderr, f"branch {ref!r}")
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        return "", None, f"ERROR: invalid JSON from gh api for branch {ref!r}"
    sha = str(d.get("sha") or "")
    if not sha:
        return "", None, f"ERROR: gh returned no sha for branch {ref!r}"
    return sha, _age_secs((d.get("commit") or {}).get("committer") or {}), ""


def _age_secs(committer: dict):
    raw = str(committer.get("date") or "").strip()
    if not raw:
        return None
    try:
        when = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return None
    return int((datetime.now(timezone.utc) - when).total_seconds())


def _run_list(ref: str):
    try:
        r = _gh(["gh", "run", "list", "--branch", ref, "--limit",
                 str(RUN_LIST_LIMIT), "--json",
                 "workflowName,headSha,databaseId,status,conclusion,event,"
                 "createdAt,attempt"] + _repo_target.gh_args())
    except FileNotFoundError:
        return None, "ERROR: gh not found — install from https://cli.github.com"
    except subprocess.TimeoutExpired:
        return None, f"ERROR: gh timed out listing runs for {ref!r}"
    if r.returncode != 0:
        return None, _format_error(r.stderr, f"workflow runs for {ref!r}")
    try:
        return json.loads(r.stdout or "[]"), ""
    except json.JSONDecodeError:
        return None, "ERROR: invalid JSON from gh run list"


def _jobs_for(run_id: int):
    """The job list for one run, or ``None`` when it did not come back.

    ``None`` and ``[]`` are different answers and stay different all the way to
    the verdict. An empty list is an established fact; a missing one is an
    absence in the tool, and counting it as zero passing legs is the guess this
    repository keeps re-filing.
    """
    try:
        r = _gh(["gh", "run", "view", str(run_id), "--json", "jobs"]
                + _repo_target.gh_args())
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    try:
        d = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return None
    jobs = d.get("jobs")
    return jobs if isinstance(jobs, list) else None


def _reconcile(repo: str, selected: dict, fetched: dict) -> tuple:
    """`(marker, lines)` squaring the legs read against the legs declared (#837).

    The second source is `jobs?filter=all`, per run, and the reasoning is in
    `presets/_declared_legs`. Branch scope adds one thing over `gh-pr`'s: the
    run ids come from the run *list*, so a workflow is reconciled whether or
    not anything it produced reached a check rollup.

    Runs whose job list never came back are skipped rather than reconciled —
    `verdict()` already answers UNKNOWN for those, and a second, differently
    worded doubt about the same absence is noise.

    The two sides are summed across workflows before `shortfall()` sees them,
    so the `Legs:` line and the marker beside it are the same arithmetic. Any
    single run that cannot be reconciled makes the whole answer unverified: a
    declared total summed over only the readable runs is smaller than the
    truth, and a smaller declared total is exactly what makes a short tally
    look complete.
    """
    owner, name = _declared_legs.owner_repo(repo)
    found_total = 0
    declared_total: int | None = 0
    missing: list = []
    for wf, run in sorted(selected.items()):
        jobs = fetched.get(wf)
        if jobs is None:
            continue
        found = [str(j.get("name") or "?") for j in jobs
                 if isinstance(j, dict)]
        found_total += len(found)
        if not _declared_legs.reconcilable(run.get("attempt")):
            declared_total = (declared_total + len(found)
                              if declared_total is not None else None)
            continue
        names = _declared_legs.legs_for_run(owner, name, _run_id(run))
        if names is None:
            declared_total = None
            continue
        if declared_total is not None:
            declared_total += len(names)
        missing.extend(f"{wf} / {n}" for n in
                       _declared_legs.missing_names(names, found))
    if not found_total and declared_total == 0:
        return ("", [])
    return _checks.shortfall(found_total, declared_total, missing)


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------

def _row(name: str, run: dict, jobs) -> str:
    phase = run_phase(run)
    if phase == PHASE_CONCLUDED:
        outcome = str(run.get("conclusion") or "no conclusion")
    elif phase == PHASE_RUNNING:
        outcome = "not yet"
    else:
        outcome = "not read"
    if jobs is None:
        tally = "UNREAD — the job list did not come back"
    else:
        tally = leg_summary([_checks.github_state(j) for j in jobs])
    return f"{name:<32} {phase:<14} {outcome:<14} {tally}"


def main() -> int:
    args = [a for a in sys.argv[1:] if a != ""]

    repo, default_branch, err = _repo_identity()
    if err:
        print(err)
        return 1

    ref = args[0] if args else default_branch
    if not ref:
        print("ERROR: no branch given and the repository's default branch "
              "could not be resolved. Name one: gh-branch:BRANCH")
        return 1

    sha, age, err = _head_commit(ref)
    if err:
        print(err)
        return 1

    runs, err = _run_list(ref)
    if err:
        print(err)
        return 1

    selected = latest_per_workflow(runs, sha)
    prev_sha, prev_names = previous_head(runs, sha)
    missing = sorted(prev_names - set(selected))

    legs: dict = {}
    named: list = []
    if selected:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(JOB_WORKERS, len(selected))) as pool:
            fetched = dict(zip(
                selected,
                pool.map(lambda n: _jobs_for(_run_id(selected[n])), selected)))
        for name, jobs in fetched.items():
            if jobs is None:
                legs[name] = None
                continue
            legs[name] = [_checks.github_state(j) for j in jobs]
            for j in jobs:
                named.append((f"{name} / {j.get('name', '?')}",
                              _checks.github_state(j),
                              "job",
                              str(j.get("databaseId") or "")))
    else:
        fetched = {}

    marker, shortfall_lines = _reconcile(repo, selected, fetched)
    state, sentence = verdict(selected, legs, missing, sha, age, _GRACE, marker)

    print(f"# Is `{ref}` green? — {repo}")
    print(f"Branch {ref}: {state}")
    print(f"Head: {sha[:7]} ({sha}) — {_duration(age)} old")
    print(f"Verdict: {sentence}")

    if selected:
        all_states = [s for v in legs.values() for s in (v or [])]
        print(f"Legs: {leg_summary(all_states)}"
              f"{' ' + marker if marker else ''}")
        for line in shortfall_lines:
            print(line)
        for line in _checks.named_disclosure(named):
            print(line)

        print()
        print(f"{'Workflow':<32} {'Run':<14} {'Outcome':<14} Legs")
        print("-" * 96)
        for name in sorted(selected):
            print(_row(name, selected[name], fetched.get(name)))

    if missing:
        print()
        print(f"Workflows on the previous head {prev_sha[:7]} with no run on "
              f"{sha[:7]}:")
        for name in missing:
            print(f"  {name} — did NOT run on this commit. That is a different "
                  "sentence from 'ran and passed'; whether a path filter "
                  "excluded it or a run is still to be created is UNKNOWN.")

    # Zero for every *established* verdict, green or not. Nonzero is reserved
    # for "this op could not answer" — the family's convention (`gh-run` exits
    # 0 on a run that concluded `failure`), and load-bearing here: supertool
    # renders a nonzero exit as `FAIL`, which would print the same banner for a
    # branch whose tests are merely still running as for a gh that is not
    # authenticated. Two different things rendering alike is the defect this op
    # exists to remove; it does not get to reintroduce it in its own exit code.
    # The verdict is the `Branch <name>: <STATE>` line, which a caller can
    # match on and a reader cannot miss.
    return 0


if __name__ == "__main__":
    sys.exit(main())
