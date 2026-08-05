"""#804 (comment) — `gh-pr:status` reconciled against the rollup, which is circular.

The live case, from the merge gate, minutes after PR #822's branch was pushed::

    #822 | state: OPEN | mergeable: CONFLICTING | conflicts: yes
    checks: 4 total: 4 passed, 0 failed, 0 pending

No `⚠ NOT ALL GREEN`, no `⚠ INCOMPLETE`. The repository's matrix is 18. #724's
reconciliation existed and did not fire, and the reason is not a bug in
`shortfall()` — it is where the declared count came from.

`_reconcile_checks` derived its run ids **from the rollup it was checking**.
Read against the real API, PR #822's rollup names two Actions runs: the CodeQL
run (3 `Analyze` legs) and the tests run (14 legs, plus one external
`github-advanced-security` check). In the window observed, only the CodeQL
run's legs had reached the rollup. So the declared side was computed over the
CodeQL run alone — 3 legs declared, 3 legs found — and a run that is entirely
absent from the rollup contributes nothing to *either* side and cancels out. A
second source read through the first one is not a second source.

The fix takes the run ids from the commit (`actions/runs?head_sha=`) instead, so
a run absent from the rollup is still on the declared side. What that cannot
reach is a run whose jobs GitHub has not created yet: it declares nothing, so
it subtracts nothing. That state is disclosed by name rather than omitted — an
omitted field reads as "nothing to report", which is the defect being fixed.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pr = _load("presets/github/pr.py", "github_pr_804c")
checks = _load("presets/_checks.py", "checks_804c")

SHA = "58a52f58af2464285a6b1e90d43b1f133509e175"
CODEQL_RUN = "30994912202"
TESTS_RUN = "30994914616"
URL = "https://github.com/o/r/pull/822"

CODEQL_LEGS = ["Analyze (actions)", "Analyze (javascript-typescript)",
               "Analyze (python)"]
TESTS_LEGS = [f"pytest (leg {i})" for i in range(14)]


class _Completed:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def _leg(name: str, run_id: str, job_id: int) -> dict:
    return {"name": name, "status": "COMPLETED", "conclusion": "SUCCESS",
            "detailsUrl": (f"https://github.com/o/r/actions/runs/{run_id}"
                           f"/job/{job_id}")}


def _pr_payload(rollup: list) -> dict:
    return {
        "number": 822, "title": "t", "state": "OPEN",
        "author": {"login": "a"}, "headRefName": "f", "baseRefName": "master",
        "headRefOid": SHA, "labels": [], "milestone": None,
        "reviewDecision": None, "reviews": [], "mergeCommit": None,
        "mergeable": "CONFLICTING", "isDraft": False, "url": URL,
        "body": "", "comments": [], "additions": 1, "deletions": 0,
        "changedFiles": 1, "statusCheckRollup": rollup, "assignees": [],
        "createdAt": "2026-08-05T10:00:00Z",
        "updatedAt": "2026-08-05T10:00:00Z",
    }


class _Gh:
    """Fake `gh` — a rollup, a run list for the commit, and per-run job lists."""

    def __init__(self, rollup: list, runs_on_sha: list,
                 declared: dict, runs_rc: int = 0) -> None:
        self.rollup = rollup
        self.runs_on_sha = runs_on_sha
        self.declared = declared
        self.runs_rc = runs_rc
        self.runs_calls: list[list[str]] = []
        self.view_argv: list[str] = []

    def __call__(self, argv, *a, **kw):
        argv = list(argv)
        joined = " ".join(argv)
        if argv[:2] == ["git", "rev-parse"]:
            return _Completed("f\n")
        if "head_sha=" in joined:
            self.runs_calls.append(argv)
            if self.runs_rc:
                return _Completed("", returncode=self.runs_rc)
            return _Completed(json.dumps(
                {"total_count": len(self.runs_on_sha),
                 "workflow_runs": self.runs_on_sha}))
        if "filter=all" in joined:
            for rid, names in self.declared.items():
                if f"runs/{rid}/jobs" in joined:
                    return _Completed(json.dumps(
                        {"total_count": len(names),
                         "jobs": [{"name": n} for n in names]}))
            return _Completed(json.dumps({"total_count": 0, "jobs": []}))
        if argv[:2] == ["gh", "api"]:            # graphql review threads
            return _Completed(json.dumps({"data": {}}))
        self.view_argv = argv
        return _Completed(json.dumps(_pr_payload(self.rollup)))


def _run_row(run_id: str, name: str, status: str = "in_progress") -> dict:
    return {"id": int(run_id), "name": name, "status": status,
            "conclusion": None, "run_attempt": 1}


def _render(monkeypatch, capsys, gh: _Gh) -> str:
    monkeypatch.setattr(pr.subprocess, "run", gh)
    monkeypatch.setattr(pr._declared_legs.subprocess, "run", gh)
    monkeypatch.setattr(sys, "argv", ["pr.py", "822", "status"])
    assert pr.main() == 0
    return capsys.readouterr().out


def _checks_line(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("checks:"):
            return line
    raise AssertionError(f"no checks: line in output:\n{out}")


# ---------------------------------------------------------------------------
# the live case
# ---------------------------------------------------------------------------

def test_a_run_absent_from_the_rollup_is_still_on_the_declared_side(
        monkeypatch, capsys) -> None:
    """PR #822 verbatim: 3 CodeQL legs read, an 17-leg commit."""
    rollup = [_leg(n, CODEQL_RUN, 100 + i) for i, n in enumerate(CODEQL_LEGS)]
    gh = _Gh(rollup,
             [_run_row(CODEQL_RUN, "CodeQL"), _run_row(TESTS_RUN, "tests")],
             {CODEQL_RUN: CODEQL_LEGS, TESTS_RUN: TESTS_LEGS})
    out = _render(monkeypatch, capsys, gh)

    assert checks.INCOMPLETE_MARK in _checks_line(out), (
        f"3 of 17 legs rendered as a complete tally:\n{out}")
    assert "3 of 17" in out, f"the two counts are not both stated:\n{out}"
    assert TESTS_LEGS[0] in out, f"the absent legs are unnamed:\n{out}"


def test_the_tally_is_never_padded_up(monkeypatch, capsys) -> None:
    rollup = [_leg(n, CODEQL_RUN, 100 + i) for i, n in enumerate(CODEQL_LEGS)]
    gh = _Gh(rollup,
             [_run_row(CODEQL_RUN, "CodeQL"), _run_row(TESTS_RUN, "tests")],
             {CODEQL_RUN: CODEQL_LEGS, TESTS_RUN: TESTS_LEGS})
    out = _render(monkeypatch, capsys, gh)
    assert "3 total" in _checks_line(out), f"the read count was rewritten:\n{out}"
    assert "17 total" not in out, f"invented legs into the tally:\n{out}"


def test_a_run_with_no_jobs_yet_is_named_not_omitted(
        monkeypatch, capsys) -> None:
    """The residual: a run that declares nothing subtracts nothing.

    Arithmetic cannot see this one — 3 found, 3 declared, reconciled — so it is
    stated in words. Omitting it would read as "nothing to report", which is
    exactly how a just-pushed 18-leg matrix rendered as `4 total: 4 passed`.
    """
    rollup = [_leg(n, CODEQL_RUN, 100 + i) for i, n in enumerate(CODEQL_LEGS)]
    gh = _Gh(rollup,
             [_run_row(CODEQL_RUN, "CodeQL"), _run_row(TESTS_RUN, "tests")],
             {CODEQL_RUN: CODEQL_LEGS, TESTS_RUN: []})
    out = _render(monkeypatch, capsys, gh)

    assert "tests" in out, f"an uncovered run is unnamed:\n{out}"
    assert checks.INCOMPLETE_MARK in _checks_line(out), (
        f"a tally covering none of a whole run rendered complete:\n{out}")


# ---------------------------------------------------------------------------
# silence when reconciled
# ---------------------------------------------------------------------------

def test_a_fully_covered_commit_is_silent(monkeypatch, capsys) -> None:
    rollup = ([_leg(n, CODEQL_RUN, 100 + i) for i, n in enumerate(CODEQL_LEGS)]
              + [_leg(n, TESTS_RUN, 200 + i) for i, n in enumerate(TESTS_LEGS)])
    gh = _Gh(rollup,
             [_run_row(CODEQL_RUN, "CodeQL"), _run_row(TESTS_RUN, "tests")],
             {CODEQL_RUN: CODEQL_LEGS, TESTS_RUN: TESTS_LEGS})
    out = _render(monkeypatch, capsys, gh)

    assert checks.INCOMPLETE_MARK not in out, f"false alarm:\n{out}"
    assert checks.UNVERIFIED_MARK not in out, f"false alarm:\n{out}"


def test_an_external_only_check_suite_is_silent(monkeypatch, capsys) -> None:
    """No Actions run anywhere on the commit: nothing to be short of."""
    rollup = [{"name": "ci/external", "state": "SUCCESS",
               "targetUrl": "https://ci.example/build/1"}]
    gh = _Gh(rollup, [], {})
    out = _render(monkeypatch, capsys, gh)

    assert checks.INCOMPLETE_MARK not in out, f"false alarm:\n{out}"
    assert checks.UNVERIFIED_MARK not in out, f"false alarm:\n{out}"


# ---------------------------------------------------------------------------
# declining
# ---------------------------------------------------------------------------

def test_an_unreadable_run_list_declines_instead_of_falling_back(
        monkeypatch, capsys) -> None:
    """Falling back to the rollup would restore the blind mechanism silently."""
    rollup = [_leg(n, CODEQL_RUN, 100 + i) for i, n in enumerate(CODEQL_LEGS)]
    gh = _Gh(rollup, [], {CODEQL_RUN: CODEQL_LEGS}, runs_rc=1)
    out = _render(monkeypatch, capsys, gh)

    assert checks.UNVERIFIED_MARK in _checks_line(out), (
        f"an unestablished run list rendered as a reconciled tally:\n{out}")
    assert checks.INCOMPLETE_MARK not in out, (
        f"claimed a shortfall it never established:\n{out}")


def test_the_head_sha_is_actually_requested(monkeypatch, capsys) -> None:
    """Without `headRefOid` there is no commit to list the runs of."""
    rollup = [_leg(n, CODEQL_RUN, 100 + i) for i, n in enumerate(CODEQL_LEGS)]
    gh = _Gh(rollup, [_run_row(CODEQL_RUN, "CodeQL")],
             {CODEQL_RUN: CODEQL_LEGS})
    _render(monkeypatch, capsys, gh)

    assert any("headRefOid" in a for a in gh.view_argv), (
        f"`headRefOid` not among the fields fetched: {gh.view_argv}")
    assert gh.runs_calls, "the commit's runs were never listed"
    assert SHA in " ".join(gh.runs_calls[0]), gh.runs_calls[0]
