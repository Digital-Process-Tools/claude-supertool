"""`gh-branch` — "is this branch green?" answered per workflow, not per recency (#615).

The op this suite pins does not exist to save keystrokes. It exists because
every hand-rolled substitute for it, including the one written in #615's own
body, reduces to:

    gh run list --branch master --limit 1

and a repo with more than one `push` workflow has **several** runs per commit.
`--limit 1` returns whichever *workflow* started most recently. On 2026-08-05
that was CodeQL, green, while the `tests` matrix for the same SHA was still
`queued` — so the reflex reports `completed success` for a commit whose test
matrix has not started. The right answer arrived by luck twice.

Every assertion below is written against the bar "would this still pass if the
code did nothing":

* the centrepiece (`test_codeql_green_while_tests_queued_is_not_green`) feeds
  exactly that two-workflow payload, CodeQL first in list order, and demands the
  op refuse to call it green — a `--limit 1` implementation fails it;
* the tally terms are parsed back out and summed against the leg count, so a
  `CANCELLED` that evaporates (the #445/#454 defect) fails the sum rather than
  passing quietly;
* zero runs, a run still moving, and a run that concluded must render as three
  distinguishable sentences, or they collapse into the one reading #585 removed;
* an unread job list must decline, never count as zero green legs.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


branch = _load("presets/github/branch.py", "github_branch_615")
checks = _load("presets/_checks.py", "checks_615")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

# Exit code of the last `_render()`. Nonzero means the op declined to answer;
# a verdict — green or red — is always 0. See the exit-code test at the foot.
_LAST_RC = 0

_HEAD = "1b402c0f2f0e4b1d9c4a5e6f70819293a4b5c6d7"
_PREV = "a13c9df1e2f3a4b5c6d7e8f90a1b2c3d4e5f6071"


class _Completed:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _run(workflow: str, sha: str, status: str, conclusion: str | None,
         run_id: int, created: str = "2026-08-05T09:00:00Z") -> dict:
    return {"workflowName": workflow, "headSha": sha, "databaseId": run_id,
            "status": status, "conclusion": conclusion, "event": "push",
            "createdAt": created}


def _job(name: str, status: str, conclusion: str | None = None) -> dict:
    return {"name": name, "status": status, "conclusion": conclusion,
            "databaseId": 900 + len(name), "steps": []}


def _iso(secs_ago: int) -> str:
    return (datetime.now(timezone.utc)
            - timedelta(seconds=secs_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _render(monkeypatch, capsys, *, runs: list[dict],
            jobs: dict[int, Any],
            head_sha: str = _HEAD,
            head_age: int = 4000,
            argv: list[str] | None = None,
            default_branch: str = "master",
            repo: str = "Digital-Process-Tools/claude-supertool",
            fail: str = "") -> str:
    """Drive the op end to end against a faked `gh`, return stdout.

    `jobs` maps a run's databaseId to its job list; a run id absent from the
    map, or mapped to None, models a job fetch that did not answer.
    """
    def fake(cmd, *a, **kw):
        argv_ = list(cmd)
        joined = " ".join(str(x) for x in argv_)
        if fail and fail in joined:
            return _Completed("", 1, "HTTP 404: Not Found")
        if argv_[:3] == ["gh", "repo", "view"]:
            return _Completed(json.dumps({
                "nameWithOwner": repo,
                "defaultBranchRef": {"name": default_branch}}))
        if argv_[:2] == ["gh", "api"]:
            return _Completed(json.dumps({
                "sha": head_sha,
                "commit": {"committer": {"date": _iso(head_age)}}}))
        if argv_[:3] == ["gh", "run", "list"]:
            return _Completed(json.dumps(runs))
        if argv_[:3] == ["gh", "run", "view"]:
            rid = int(argv_[3])
            payload = jobs.get(rid, "__missing__")
            if payload == "__missing__":
                return _Completed("", 1, "HTTP 500")
            if payload is None:
                return _Completed(json.dumps({}))
            return _Completed(json.dumps({"jobs": payload}))
        return _Completed("", 1, "unexpected call: " + joined)

    monkeypatch.setattr(branch.subprocess, "run", fake)
    monkeypatch.setattr(sys, "argv", ["branch.py"] + (argv or []))
    global _LAST_RC
    _LAST_RC = branch.main()
    return capsys.readouterr().out


def _line(out: str, prefix: str) -> str:
    for line in out.splitlines():
        if line.startswith(prefix):
            return line
    raise AssertionError(f"no {prefix!r} line in output:\n{out}")


def _state(out: str) -> str:
    """The one state token off the `Branch <name>: <STATE>` line.

    Parsed exactly rather than matched as a substring: `NOT GREEN` contains
    `GREEN`, so a substring test cannot tell the verdict from its negation —
    which is the discrimination this whole suite exists to make.
    """
    line = _line(out, "Branch")
    m = re.match(r"Branch .+?: (.+)$", line)
    assert m, f"unparseable branch line: {line}"
    return m.group(1).strip()


_TERM = re.compile(r"(\d+) ([a-z_]+)")


def _sum_terms(line: str) -> tuple[int, int]:
    """`(declared total, sum of the terms after it)` from a summarize() line."""
    m = re.search(r"(\d+) total: (.+)", line)
    assert m, f"no `N total:` tally in: {line}"
    tail = m.group(2).split("(")[0].split("⚠")[0]
    return int(m.group(1)), sum(int(n) for n, _ in _TERM.findall(tail))


# The payload from #615 comment 2, verbatim in shape: two workflows on one SHA,
# CodeQL listed first because it started last, green — and `tests` queued.
_CODEQL_FIRST = [
    _run("CodeQL", _HEAD, "completed", "success", 3001,
         created="2026-08-05T09:10:00Z"),
    _run("tests", _HEAD, "queued", None, 3002,
         created="2026-08-05T09:09:00Z"),
    _run("CodeQL", _PREV, "completed", "success", 2001),
    _run("tests", _PREV, "completed", "success", 2002),
]

_CODEQL_FIRST_JOBS = {
    3001: [_job("Analyze (python)", "completed", "success")],
    3002: [_job(f"pytest ({i})", "queued") for i in range(4)],
    2001: [_job("Analyze (python)", "completed", "success")],
    2002: [_job("pytest", "completed", "success")],
}


# ---------------------------------------------------------------------------
# the centrepiece: workflow identity, not recency
# ---------------------------------------------------------------------------

def test_codeql_green_while_tests_queued_is_not_green(monkeypatch, capsys) -> None:
    """#615 comment 2 verbatim. A `--limit 1` implementation fails this."""
    out = _render(monkeypatch, capsys,
                  runs=_CODEQL_FIRST, jobs=_CODEQL_FIRST_JOBS)

    assert _state(out) == branch.NOT_GREEN, (
        "the op called a commit green whose test matrix has not started:\n" + out)

    verdict = _line(out, "Verdict:")
    assert "tests" in verdict, (
        "the workflow that is holding the commit back is not named: " + verdict)


def test_a_second_workflows_conclusion_cannot_stand_in_for_the_commit(
        monkeypatch, capsys) -> None:
    """Both workflows on the SHA are read, not just the most recent one."""
    out = _render(monkeypatch, capsys,
                  runs=_CODEQL_FIRST, jobs=_CODEQL_FIRST_JOBS)

    assert "CodeQL" in out and "tests" in out, (
        "only one workflow reached the output — this is the `--limit 1` "
        "defect the op exists to remove:\n" + out)
    # …and the green one is not what the headline reports.
    assert "success" not in _line(out, "Branch").lower()


def test_the_queued_workflow_is_not_reported_as_a_failure(
        monkeypatch, capsys) -> None:
    """Pending and failed are different findings and must read differently."""
    out = _render(monkeypatch, capsys,
                  runs=_CODEQL_FIRST, jobs=_CODEQL_FIRST_JOBS)
    verdict = _line(out, "Verdict:")
    assert "0 failed" in _line(out, "Legs:") or "failed" not in verdict.lower(), (
        "a queued workflow was reported as a failure: " + verdict)


# ---------------------------------------------------------------------------
# arithmetic — the terms sum to the leg count, across workflows
# ---------------------------------------------------------------------------

def test_leg_tally_sums_across_every_workflow_on_the_sha(
        monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys,
                  runs=_CODEQL_FIRST, jobs=_CODEQL_FIRST_JOBS)
    total, summed = _sum_terms(_line(out, "Legs:"))
    assert total == 5, "1 CodeQL leg + 4 pytest legs were not all counted"
    assert summed == 5


def test_a_cancelled_leg_is_named_and_counted_not_absorbed(
        monkeypatch, capsys) -> None:
    """#454's defect, re-pointed at a branch: `0 failed, 0 pending` while two
    legs were CANCELLED."""
    runs = [_run("tests", _HEAD, "completed", "failure", 4001)]
    jobs = {4001: [_job("a", "completed", "success"),
                   _job("b", "completed", "success"),
                   _job("c", "completed", "cancelled"),
                   _job("d", "completed", "cancelled")]}
    out = _render(monkeypatch, capsys, runs=runs, jobs=jobs)

    legs = _line(out, "Legs:")
    total, summed = _sum_terms(legs)
    assert total == 4 and summed == 4, (
        "two CANCELLED legs evaporated from the tally: " + legs)
    assert "2 cancelled" in legs
    assert _state(out) == branch.NOT_GREEN
    assert "c" in out and "d" in out, "the cancelled legs were not named:\n" + out


def test_a_failed_leg_is_named_with_its_workflow(monkeypatch, capsys) -> None:
    runs = [_run("tests", _HEAD, "completed", "failure", 5001),
            _run("CodeQL", _HEAD, "completed", "success", 5002)]
    jobs = {5001: [_job("pytest (windows-latest, 3.11)", "completed", "failure"),
                   _job("pytest (ubuntu-latest, 3.12)", "completed", "success")],
            5002: [_job("Analyze (python)", "completed", "success")]}
    out = _render(monkeypatch, capsys, runs=runs, jobs=jobs)

    assert _state(out) == branch.NOT_GREEN
    assert "pytest (windows-latest, 3.11)" in out, (
        "the failing leg was absorbed into a count:\n" + out)
    assert "tests" in _line(out, "Verdict:")


# ---------------------------------------------------------------------------
# green
# ---------------------------------------------------------------------------

def test_every_workflow_concluded_and_every_leg_passed_is_green(
        monkeypatch, capsys) -> None:
    runs = [_run("CodeQL", _HEAD, "completed", "success", 6001),
            _run("tests", _HEAD, "completed", "success", 6002)]
    jobs = {6001: [_job("Analyze (python)", "completed", "success")],
            6002: [_job("pytest (ubuntu)", "completed", "success"),
                   _job("pytest (macos)", "completed", "success")]}
    out = _render(monkeypatch, capsys, runs=runs, jobs=jobs)

    assert _state(out) == branch.GREEN
    total, summed = _sum_terms(_line(out, "Legs:"))
    assert (total, summed) == (3, 3)


def test_green_names_the_head_sha_it_is_a_claim_about(
        monkeypatch, capsys) -> None:
    """"The branch is green" is a claim about a commit (#615)."""
    runs = [_run("tests", _HEAD, "completed", "success", 6002)]
    jobs = {6002: [_job("pytest", "completed", "success")]}
    out = _render(monkeypatch, capsys, runs=runs, jobs=jobs)
    assert _HEAD[:7] in out, "the head SHA is not named:\n" + out


# ---------------------------------------------------------------------------
# the third state — no run for this ref, with the reason
# ---------------------------------------------------------------------------

def test_no_run_on_the_head_sha_inside_the_grace_window_says_still_expected(
        monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys,
                  runs=[_run("tests", _PREV, "completed", "success", 7001)],
                  jobs={7001: [_job("pytest", "completed", "success")]},
                  head_age=60)

    assert _state(out) == branch.NO_RUN, out
    assert "expected" in out.lower()


def test_no_run_past_the_grace_window_declines_rather_than_concluding(
        monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys,
                  runs=[_run("tests", _PREV, "completed", "success", 7001)],
                  jobs={7001: [_job("pytest", "completed", "success")]},
                  head_age=48 * 3600)

    assert _state(out) == branch.NO_RUN
    assert "UNKNOWN" in out, (
        "an absence past the window was concluded rather than declined:\n" + out)


def test_the_two_no_run_readings_do_not_render_alike(
        monkeypatch, capsys) -> None:
    """Zero must not read the same as "not yet" — #615's most-filed class."""
    common = dict(runs=[_run("tests", _PREV, "completed", "success", 7001)],
                  jobs={7001: [_job("pytest", "completed", "success")]})
    young = _line(_render(monkeypatch, capsys, head_age=60, **common), "Verdict:")
    old = _line(_render(monkeypatch, capsys, head_age=48 * 3600, **common),
                "Verdict:")
    assert young != old


def test_a_branch_with_no_runs_at_all_is_not_green(monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, runs=[], jobs={}, head_age=48 * 3600)
    assert _state(out) == branch.NO_RUN


# ---------------------------------------------------------------------------
# concluded must be unmistakably distinct from still running (comment 1)
# ---------------------------------------------------------------------------

def test_a_concluded_workflow_and_a_running_one_read_differently(
        monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys,
                  runs=_CODEQL_FIRST, jobs=_CODEQL_FIRST_JOBS)
    rows = [ln for ln in out.splitlines()
            if ln.startswith("CodeQL") or ln.startswith("tests")]
    assert len(rows) == 2, "expected one row per workflow:\n" + out
    codeql, tests = (rows[0], rows[1]) if rows[0].startswith("CodeQL") else (rows[1], rows[0])

    assert branch.PHASE_CONCLUDED in codeql, codeql
    assert branch.PHASE_CONCLUDED not in tests, (
        "a queued workflow reads as concluded: " + tests)
    assert branch.PHASE_CONCLUDED != branch.PHASE_RUNNING


def test_no_bare_token_in_a_row_can_be_misread_as_a_leg_state(
        monkeypatch, capsys) -> None:
    """Comment 1: `[time]` was read as a possible TIMED_OUT.

    Every row states its phase in a word this module owns, so a reader never
    has to decide whether a bare column is a status.
    """
    out = _render(monkeypatch, capsys,
                  runs=_CODEQL_FIRST, jobs=_CODEQL_FIRST_JOBS)
    for ln in out.splitlines():
        if ln.startswith(("CodeQL", "tests")):
            assert (branch.PHASE_CONCLUDED in ln or branch.PHASE_RUNNING in ln
                    or branch.PHASE_UNESTABLISHED in ln), ln


# ---------------------------------------------------------------------------
# unread is not zero
# ---------------------------------------------------------------------------

def test_an_unreadable_job_list_declines_instead_of_counting_zero(
        monkeypatch, capsys) -> None:
    """docs/validators.md §Declining instead of guessing, at branch scope."""
    runs = [_run("tests", _HEAD, "completed", "success", 8001),
            _run("CodeQL", _HEAD, "completed", "success", 8002)]
    jobs = {8002: [_job("Analyze", "completed", "success")]}  # 8001 unreadable
    out = _render(monkeypatch, capsys, runs=runs, jobs=jobs)

    assert _state(out) == branch.UNKNOWN, out
    assert "tests" in out


def test_a_run_payload_with_no_job_list_is_unread_not_empty(
        monkeypatch, capsys) -> None:
    runs = [_run("tests", _HEAD, "completed", "success", 8003)]
    out = _render(monkeypatch, capsys, runs=runs, jobs={8003: None})
    assert _state(out) == branch.UNKNOWN


# ---------------------------------------------------------------------------
# a workflow that ran on the previous head and not on this one
# ---------------------------------------------------------------------------

def test_a_workflow_missing_since_the_previous_head_is_not_green_while_young(
        monkeypatch, capsys) -> None:
    runs = [_run("CodeQL", _HEAD, "completed", "success", 9001),
            _run("CodeQL", _PREV, "completed", "success", 9002),
            _run("tests", _PREV, "completed", "success", 9003)]
    jobs = {9001: [_job("Analyze", "completed", "success")],
            9002: [_job("Analyze", "completed", "success")],
            9003: [_job("pytest", "completed", "success")]}
    out = _render(monkeypatch, capsys, runs=runs, jobs=jobs, head_age=60)

    assert _state(out) != branch.GREEN
    assert "tests" in out, (
        "a workflow present on the previous head and absent here went "
        "unmentioned:\n" + out)


def test_the_same_absence_past_the_window_is_a_note_not_a_verdict(
        monkeypatch, capsys) -> None:
    """Path filters are legitimate; a permanent not-green would be wrong."""
    runs = [_run("CodeQL", _HEAD, "completed", "success", 9001),
            _run("CodeQL", _PREV, "completed", "success", 9002),
            _run("tests", _PREV, "completed", "success", 9003)]
    jobs = {9001: [_job("Analyze", "completed", "success")],
            9002: [_job("Analyze", "completed", "success")],
            9003: [_job("pytest", "completed", "success")]}
    out = _render(monkeypatch, capsys, runs=runs, jobs=jobs, head_age=48 * 3600)

    assert branch.GREEN in _line(out, "Branch")
    assert "tests" in out, "the observation was dropped entirely:\n" + out


# ---------------------------------------------------------------------------
# run selection
# ---------------------------------------------------------------------------

def test_latest_per_workflow_keeps_one_run_per_workflow_on_the_sha() -> None:
    runs = [_run("tests", _HEAD, "completed", "failure", 10),
            _run("tests", _HEAD, "completed", "success", 20),
            _run("CodeQL", _HEAD, "completed", "success", 15),
            _run("tests", _PREV, "completed", "success", 30)]
    selected = branch.latest_per_workflow(runs, _HEAD)

    assert set(selected) == {"tests", "CodeQL"}
    assert selected["tests"]["databaseId"] == 20, (
        "a re-run did not supersede the run it replaced")


def test_latest_per_workflow_ignores_other_shas() -> None:
    runs = [_run("tests", _PREV, "completed", "success", 30)]
    assert branch.latest_per_workflow(runs, _HEAD) == {}


def test_run_phase_distinguishes_concluded_from_moving() -> None:
    assert branch.run_phase(_run("t", _HEAD, "completed", "success", 1)) == \
        branch.PHASE_CONCLUDED
    assert branch.run_phase(_run("t", _HEAD, "in_progress", None, 1)) == \
        branch.PHASE_RUNNING
    assert branch.run_phase(_run("t", _HEAD, "queued", None, 1)) == \
        branch.PHASE_RUNNING
    assert branch.run_phase({"status": ""}) == branch.PHASE_UNESTABLISHED


# ---------------------------------------------------------------------------
# targeting and defaults
# ---------------------------------------------------------------------------

def test_no_argument_answers_for_the_repos_default_branch(
        monkeypatch, capsys) -> None:
    runs = [_run("tests", _HEAD, "completed", "success", 11001)]
    jobs = {11001: [_job("pytest", "completed", "success")]}
    out = _render(monkeypatch, capsys, runs=runs, jobs=jobs,
                  argv=[], default_branch="trunk")
    assert "trunk" in _line(out, "Branch"), (
        "the no-argument form did not resolve the default branch:\n" + out)


def test_an_explicit_branch_argument_wins(monkeypatch, capsys) -> None:
    runs = [_run("tests", _HEAD, "completed", "success", 11001)]
    jobs = {11001: [_job("pytest", "completed", "success")]}
    out = _render(monkeypatch, capsys, runs=runs, jobs=jobs,
                  argv=["release/1.2"], default_branch="master")
    assert "release/1.2" in _line(out, "Branch")


def test_repo_target_reaches_both_gh_routes(monkeypatch, capsys) -> None:
    """#677: `repo:OWNER/NAME` must reach `gh run list` and `gh api` alike."""
    seen: list[str] = []

    def fake(cmd, *a, **kw):
        argv_ = list(cmd)
        seen.append(" ".join(str(x) for x in argv_))
        if argv_[:3] == ["gh", "repo", "view"]:
            return _Completed(json.dumps({
                "nameWithOwner": "o/n", "defaultBranchRef": {"name": "master"}}))
        if argv_[:2] == ["gh", "api"]:
            return _Completed(json.dumps({
                "sha": _HEAD,
                "commit": {"committer": {"date": _iso(4000)}}}))
        if argv_[:3] == ["gh", "run", "list"]:
            return _Completed(json.dumps(
                [_run("tests", _HEAD, "completed", "success", 12001)]))
        return _Completed(json.dumps(
            {"jobs": [_job("pytest", "completed", "success")]}))

    monkeypatch.setenv("SUPERTOOL_REPO", "o/n")
    monkeypatch.setattr(branch.subprocess, "run", fake)
    monkeypatch.setattr(sys, "argv", ["branch.py", "master"])
    branch.main()
    capsys.readouterr()

    run_list = [c for c in seen if c.startswith("gh run list")]
    api = [c for c in seen if c.startswith("gh api")]
    assert run_list and "--repo o/n" in run_list[0], run_list
    assert api and "repos/o/n/" in api[0], api


def test_an_unresolvable_branch_is_an_error_not_a_green(
        monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, runs=[], jobs={},
                  argv=["nope"], fail="gh api")
    assert branch.GREEN not in out
    assert "ERROR" in out
    assert _LAST_RC == 1, (
        "an op that could not answer exited 0 — indistinguishable from a "
        "verdict it never reached")


# ---------------------------------------------------------------------------
# exit code — an answer is an answer, green or not
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("runs,jobs,green", [
    ([_run("tests", _HEAD, "completed", "success", 1)],
     {1: [_job("pytest", "completed", "success")]}, True),
    ([_run("tests", _HEAD, "completed", "failure", 1)],
     {1: [_job("pytest", "completed", "failure")]}, False),
])
def test_an_established_verdict_exits_zero_green_or_not(monkeypatch, capsys,
                                                        runs, jobs,
                                                        green) -> None:
    """Nonzero is reserved for "could not answer".

    supertool renders a nonzero exit as `FAIL`. Exiting 1 on a red branch would
    print the same banner for "tests are still running" as for "gh is not
    authenticated" — two different things rendering alike, which is the defect
    this op exists to remove.
    """
    def fake(cmd, *a, **kw):
        argv_ = list(cmd)
        if argv_[:3] == ["gh", "repo", "view"]:
            return _Completed(json.dumps({
                "nameWithOwner": "o/n", "defaultBranchRef": {"name": "master"}}))
        if argv_[:2] == ["gh", "api"]:
            return _Completed(json.dumps({
                "sha": _HEAD,
                "commit": {"committer": {"date": _iso(4000)}}}))
        if argv_[:3] == ["gh", "run", "list"]:
            return _Completed(json.dumps(runs))
        return _Completed(json.dumps({"jobs": jobs[int(argv_[3])]}))

    monkeypatch.setattr(branch.subprocess, "run", fake)
    monkeypatch.setattr(sys, "argv", ["branch.py", "master"])
    rc = branch.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert _state(out) == (branch.GREEN if green else branch.NOT_GREEN)


# ---------------------------------------------------------------------------
# the tally is the shared one, not a second derivation
# ---------------------------------------------------------------------------

def test_the_leg_tally_comes_from_the_shared_checks_module() -> None:
    """One place where a CANCELLED can be mis-tallied — #615's own argument."""
    states = ["SUCCESS", "SUCCESS", "CANCELLED"]
    assert branch.leg_summary(states) == checks.summarize(states)

# ---------------------------------------------------------------------------
# the four cases the mutation run found nothing constraining
#
# Each of these isolates ONE red signal, so the clause that reads it is the only
# thing standing between the payload and a green. Without them the conjunction
# passed its tests through a second signal that happened to be present in the
# same fixture.
# ---------------------------------------------------------------------------

def test_a_pending_leg_blocks_green_even_on_a_concluded_run(
        monkeypatch, capsys) -> None:
    """`gh-run`'s #789 disagreement, at branch scope: GitHub calls the run
    complete while a leg still reads as running."""
    runs = [_run("tests", _HEAD, "completed", "success", 13001)]
    jobs = {13001: [_job("a", "completed", "success"),
                    _job("b", "completed", "success"),
                    _job("c", "in_progress")]}
    out = _render(monkeypatch, capsys, runs=runs, jobs=jobs)

    assert _state(out) == branch.NOT_GREEN, out
    total, summed = _sum_terms(_line(out, "Legs:"))
    assert (total, summed) == (3, 3)
    assert "1 pending" in _line(out, "Legs:")


def test_an_unconcluded_run_blocks_green_even_with_every_leg_passing(
        monkeypatch, capsys) -> None:
    """Observed live on this repo's `master` while building the op: 17 of 17
    legs `success`, and the `tests` run still `in_progress`.

    A "all legs green means green" implementation says green here and is wrong:
    the tally structurally cannot see a leg GitHub has not created yet, so an
    unfinished run may still grow one (a `needs:`-gated job appears only once
    its dependency finishes).
    """
    runs = [_run("tests", _HEAD, "in_progress", None, 13002)]
    jobs = {13002: [_job("a", "completed", "success"),
                    _job("b", "completed", "success")]}
    out = _render(monkeypatch, capsys, runs=runs, jobs=jobs)

    assert _state(out) == branch.NOT_GREEN, out
    assert "0 pending" in _line(out, "Legs:"), (
        "the fixture must have no pending leg, or it is not the run-level "
        "clause being tested: " + _line(out, "Legs:"))


def test_a_red_run_conclusion_blocks_green_even_with_every_leg_passing(
        monkeypatch, capsys) -> None:
    """The run's own verdict is read, not only its jobs'.

    A run can conclude `failure` with every job it created reporting success —
    a startup failure, or a required job GitHub never got to create.
    """
    runs = [_run("tests", _HEAD, "completed", "failure", 13003)]
    jobs = {13003: [_job("a", "completed", "success")]}
    out = _render(monkeypatch, capsys, runs=runs, jobs=jobs)

    assert _state(out) == branch.NOT_GREEN, out
    assert "tests" in _line(out, "Verdict:")


def test_a_cancelled_leg_alone_blocks_green(monkeypatch, capsys) -> None:
    """#454's leg, isolated: the run concluded `success` and the only red
    signal is a `CANCELLED` job — the state that used to evaporate."""
    runs = [_run("tests", _HEAD, "completed", "success", 13004)]
    jobs = {13004: [_job("a", "completed", "success"),
                    _job("b", "completed", "cancelled")]}
    out = _render(monkeypatch, capsys, runs=runs, jobs=jobs)

    assert _state(out) == branch.NOT_GREEN, out
    legs = _line(out, "Legs:")
    total, summed = _sum_terms(legs)
    assert (total, summed) == (2, 2)
    assert "1 cancelled" in legs
