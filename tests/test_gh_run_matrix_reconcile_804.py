"""#804 — `gh-run` tallies `filter=latest`, which dips during a partial re-run.

Measured live on this repo, run 30997282630, re-running its one failed leg with
`gh run rerun --failed` while sampling every ~2s::

    15:57:31  run_view=0   latest=0   all_distinct=14
    15:57:39  run_view=9   latest=9   all_distinct=14
    15:57:49  run_view=14  latest=14  all_distinct=14

`gh run view --json jobs` issues
`repos/{o}/{r}/actions/runs/{id}/jobs?per_page=100` with no filter — read off
`GH_DEBUG=api`, not inferred — and GitHub defaults that endpoint to
`filter=latest`. It is the same source #724 caught dipping for `gh-pr:status`.
For ~18s it hands back a strict subset of the matrix, and what `gh-run` prints
off it is internally consistent and externally short.

These tests pin the *reconciliation and its cost*, not the prose:

* a short set must be disclosed as short, naming what is absent;
* a full set must stay silent, or the marker means nothing;
* the tally must never be padded up to the matrix — disclosure, not correction;
* the second source must not be paid for on a first attempt, where it is
  provably the same set — the issue's cost question, pinned;
* a second source that cannot be read must decline, never guess either way.
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


run = _load("presets/github/run.py", "github_run_804")
checks = _load("presets/_checks.py", "checks_804")

RUN_ID = "30997282630"
MATRIX = [f"pytest (leg {i})" for i in range(14)]


class _Completed:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def _job(name: str, status: str = "completed",
         conclusion: str | None = "success") -> dict:
    return {"name": name, "status": status, "conclusion": conclusion,
            "databaseId": 4242, "steps": []}


def _payload(jobs: Any, attempt: int | None = 2, status: str = "in_progress",
             conclusion: str | None = None, omit_jobs: bool = False) -> dict:
    d: dict[str, Any] = {
        "databaseId": int(RUN_ID), "name": "tests", "status": status,
        "conclusion": conclusion, "event": "pull_request",
        "headBranch": "fix/804-run-reconcile",
        "url": f"https://github.com/o/r/actions/runs/{RUN_ID}",
    }
    if attempt is not None:
        d["attempt"] = attempt
    if not omit_jobs:
        d["jobs"] = jobs
    return d


class _Gh:
    """Fake `gh`, counting the calls the reconciliation costs."""

    def __init__(self, payload: dict, all_names: list[str] | None,
                 api_rc: int = 0) -> None:
        self.payload = payload
        self.all_names = all_names
        self.api_rc = api_rc
        self.api_calls: list[list[str]] = []
        self.view_argv: list[str] = []
        self.argvs: list[list[str]] = []

    def __call__(self, argv, *a, **kw):
        argv = list(argv)
        self.argvs.append(argv)
        if argv[:2] == ["git", "rev-parse"]:
            return _Completed("master\n")
        if argv[:3] == ["git", "worktree", "list"]:
            # `_branch_locale` asks where the run's branch is checked out
            # (#850). Empty porcelain = held by no worktree, which is the
            # shape these fixtures already assumed.
            return _Completed("")
        if argv[:2] == ["gh", "api"]:
            self.api_calls.append(argv)
            if self.api_rc:
                return _Completed("", returncode=self.api_rc)
            jobs = [{"name": n} for n in (self.all_names or [])]
            return _Completed(json.dumps({"total_count": len(jobs),
                                          "jobs": jobs}))
        if argv[:3] == ["gh", "run", "view"]:
            self.view_argv = argv
            return _Completed(json.dumps(self.payload))
        # Anything else is a command this fixture has not been taught. The
        # fall-through used to answer it with the run payload *and* record it
        # as the view call — which is how #850's `git worktree list` came to be
        # parsed as run JSON and then reported as "the fields fetched". An
        # unstubbed command is a failure now, not a wrong answer.
        raise AssertionError(f"unstubbed command: {argv!r}")


def _render(monkeypatch, capsys, gh: _Gh) -> str:
    monkeypatch.setattr(run.subprocess, "run", gh)
    monkeypatch.setattr(run._declared_legs.subprocess, "run", gh)
    monkeypatch.setattr(sys, "argv", ["run.py", RUN_ID])
    assert run.main() == 0
    return capsys.readouterr().out


def _status_line(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("Status:"):
            return line
    raise AssertionError(f"no Status: line in output:\n{out}")


# ---------------------------------------------------------------------------
# the measured dip
# ---------------------------------------------------------------------------

def test_the_measured_dip_is_disclosed_as_short(monkeypatch, capsys) -> None:
    """15:57:39 verbatim — 9 legs read against a 14-leg matrix."""
    jobs = ([_job(n) for n in MATRIX[:5]]
            + [_job(n, "queued", None) for n in MATRIX[5:9]])
    gh = _Gh(_payload(jobs), MATRIX)
    out = _render(monkeypatch, capsys, gh)

    assert checks.INCOMPLETE_MARK in _status_line(out), (
        f"a 9-of-14 tally rendered with no incompleteness marker:\n{out}")
    assert "9 of 14" in out, f"the two counts are not both stated:\n{out}"


def test_the_absent_legs_are_named(monkeypatch, capsys) -> None:
    """A gap a reader cannot act on is a gap they will ignore."""
    jobs = [_job(n) for n in MATRIX[:9]]
    out = _render(monkeypatch, capsys, _Gh(_payload(jobs), MATRIX))
    assert MATRIX[9] in out, f"leg absent from the tally is unnamed:\n{out}"


def test_zero_legs_mid_recreation_does_not_claim_none_ran(
        monkeypatch, capsys) -> None:
    """15:57:31 verbatim, and the worst line the op can print.

    `completed failure, and zero legs ran — GitHub created no job for this
    run, so nothing was tested` is asserted as established fact. During the
    dip it is a falsehood contradicted by data the op can reach: fourteen
    legs ran, and `filter=all` still names every one of them.
    """
    gh = _Gh(_payload([], status="completed", conclusion="failure"), MATRIX)
    out = _render(monkeypatch, capsys, gh)

    assert "created no job for this run" not in out, (
        f"asserted a falsehood the second source disproves:\n{out}")
    assert "nothing was tested" not in out, (
        f"asserted a falsehood the second source disproves:\n{out}")
    assert "0 of 14" in out, f"the two counts are not both stated:\n{out}"


def test_zero_legs_still_says_none_ran_when_nothing_contradicts_it(
        monkeypatch, capsys) -> None:
    """The opposite error: a genuine empty run must keep its verdict.

    Without this the fix would trade one silence for another — a run that
    really did create no job would stop saying so.
    """
    gh = _Gh(_payload([], status="completed", conclusion="failure"), [])
    out = _render(monkeypatch, capsys, gh)
    assert "created no job for this run" in out, (
        f"an established empty run lost its verdict:\n{out}")


# ---------------------------------------------------------------------------
# silence when reconciled — a marker that always fires says nothing
# ---------------------------------------------------------------------------

def test_a_full_matrix_is_silent(monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys,
                  _Gh(_payload([_job(n) for n in MATRIX]), MATRIX))
    assert checks.INCOMPLETE_MARK not in out, f"false alarm:\n{out}"
    assert checks.UNVERIFIED_MARK not in out, f"false alarm:\n{out}"


def test_extra_legs_are_not_a_shortfall(monkeypatch, capsys) -> None:
    """More read than declared is extra, never missing — and never padded."""
    jobs = [_job(n) for n in MATRIX] + [_job("external/coverage")]
    out = _render(monkeypatch, capsys, _Gh(_payload(jobs), MATRIX))
    assert checks.INCOMPLETE_MARK not in out, f"false alarm:\n{out}"
    assert "15 total" in out, f"the read count was rewritten:\n{out}"


def test_the_tally_is_never_padded_up_to_the_matrix(
        monkeypatch, capsys) -> None:
    """Disclosure, not correction: the header still counts what was read."""
    jobs = [_job(n) for n in MATRIX[:9]]
    out = _render(monkeypatch, capsys, _Gh(_payload(jobs), MATRIX))
    assert "9 total" in out, f"invented legs into the tally:\n{out}"
    assert "14 total" not in out, f"invented legs into the tally:\n{out}"


# ---------------------------------------------------------------------------
# the cost question (#804: "does this cost an extra API call per render?")
# ---------------------------------------------------------------------------

def test_a_first_attempt_costs_no_extra_call(monkeypatch, capsys) -> None:
    """On attempt 1 `filter=all` *is* `filter=latest` — nothing to buy."""
    gh = _Gh(_payload([_job(n) for n in MATRIX[:9]], attempt=1), MATRIX)
    out = _render(monkeypatch, capsys, gh)

    assert gh.api_calls == [], (
        f"paid for a second source that cannot differ: {gh.api_calls}")
    assert checks.INCOMPLETE_MARK not in out, f"false alarm:\n{out}"
    assert checks.UNVERIFIED_MARK not in out, f"false alarm:\n{out}"


def test_a_re_run_costs_exactly_one_extra_call(monkeypatch, capsys) -> None:
    gh = _Gh(_payload([_job(n) for n in MATRIX[:9]], attempt=2), MATRIX)
    _render(monkeypatch, capsys, gh)
    assert len(gh.api_calls) == 1, f"call count: {gh.api_calls}"
    argv = " ".join(gh.api_calls[0])
    assert "filter=all" in argv, (
        f"reconciled against a source that dips with the tally: {argv}")
    assert f"runs/{RUN_ID}/jobs" in argv, argv


def test_the_attempt_field_is_actually_requested(monkeypatch, capsys) -> None:
    """Without it every render looks like a re-run and pays for one."""
    gh = _Gh(_payload([_job(n) for n in MATRIX]), MATRIX)
    _render(monkeypatch, capsys, gh)
    assert any("attempt" in a for a in gh.view_argv), (
        f"`attempt` not among the fields fetched: {gh.view_argv}")


def test_an_unread_job_list_buys_nothing(monkeypatch, capsys) -> None:
    """Nothing was tallied, so there is no tally to reconcile."""
    gh = _Gh(_payload(None, omit_jobs=True), MATRIX)
    out = _render(monkeypatch, capsys, gh)
    assert gh.api_calls == [], f"reconciled an absent tally: {gh.api_calls}"
    assert "UNKNOWN" in _status_line(out), out


# ---------------------------------------------------------------------------
# declining
# ---------------------------------------------------------------------------

def test_an_unreadable_second_source_declines(monkeypatch, capsys) -> None:
    """Not silence (that is the defect) and not a number (that is a guess)."""
    gh = _Gh(_payload([_job(n) for n in MATRIX[:9]]), MATRIX, api_rc=1)
    out = _render(monkeypatch, capsys, gh)

    assert checks.UNVERIFIED_MARK in _status_line(out), (
        f"a failed second source rendered as a reconciled tally:\n{out}")
    assert checks.INCOMPLETE_MARK not in out, (
        f"claimed a shortfall it never established:\n{out}")


def test_declared_legs_counts_distinct_names_not_job_records(
        monkeypatch) -> None:
    """`filter=all` returns every attempt's rows; legs are the distinct names.

    28 rows across two attempts of a fourteen-leg matrix is this repo's real
    shape (run 30985591174). Counting rows would report a complete tally as a
    14-leg shortfall, and a disclosure that cries wolf is one nobody reads.
    """
    rows = MATRIX + MATRIX
    gh = _Gh(_payload([_job(n) for n in MATRIX]), rows)
    monkeypatch.setattr(run._declared_legs.subprocess, "run", gh)

    total, missing = run.declared_legs(
        f"https://github.com/o/r/actions/runs/{RUN_ID}", RUN_ID, 3, [])
    assert total == 14, f"counted job records, not legs: {total}"
    assert len(missing) == 14


def test_declared_legs_declines_rather_than_returning_a_floor(
        monkeypatch) -> None:
    """A guessed floor can sit under the real one — the defect in a fix's suit."""
    def fake(argv, *a, **kw):
        return _Completed("not json at all")

    monkeypatch.setattr(run._declared_legs.subprocess, "run", fake)
    assert run.declared_legs(
        f"https://github.com/o/r/actions/runs/{RUN_ID}", RUN_ID, 2, []
    ) == (None, [])
