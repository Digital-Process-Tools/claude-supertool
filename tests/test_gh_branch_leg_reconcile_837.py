"""#837 — `gh-branch`'s leg count had no second source, so a short list read green.

`gh-branch` fetches each selected run's jobs with `gh run view --json jobs` and
counts what comes back. That call is `jobs?filter=latest`, measured dipping to a
strict subset of the matrix for ~18s after a partial re-run (see
`presets/_declared_legs`, and #804 for the samples). The arithmetic still sums,
every state is accounted for, and the verdict rendered::

    Branch master: GREEN
    Verdict: GREEN — every workflow on c629679 concluded and every leg passed
             (17 legs across 2 workflows).

True about the legs it read; silent about the legs it did not. This is the merge
gate the op was built to make trustworthy, so a green it cannot reconcile must
stop being a green.

Pinned here: the shortfall downgrades an otherwise-green verdict, a reconciled
run stays silent and stays GREEN, the leg count is never padded up to the
declared one, and the reconciliation is not paid for on a first attempt.
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


branch = _load("presets/github/branch.py", "github_branch_837")
checks = _load("presets/_checks.py", "checks_837")

SHA = "c629679aa1f4c0d2f7b9e8a3d5c1b0e2f4a6c8d0"
RUN_ID = 30997282630
MATRIX = [f"pytest (leg {i})" for i in range(14)]


class _Completed:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


class _Gh:
    """Fake `gh` for the whole `gh-branch` call chain."""

    def __init__(self, job_names: list[str], declared: list[str] | None,
                 attempt: int = 2, api_rc: int = 0) -> None:
        self.job_names = job_names
        self.declared = declared
        self.attempt = attempt
        self.api_rc = api_rc
        self.declared_calls: list[list[str]] = []

    def __call__(self, argv, *a, **kw):
        argv = list(argv)
        joined = " ".join(argv)
        if argv[:3] == ["gh", "repo", "view"]:
            return _Completed(json.dumps({
                "nameWithOwner": "o/r",
                "defaultBranchRef": {"name": "master"}}))
        if "filter=all" in joined:
            self.declared_calls.append(argv)
            if self.api_rc:
                return _Completed("", returncode=self.api_rc)
            names = self.declared or []
            return _Completed(json.dumps(
                {"total_count": len(names),
                 "jobs": [{"name": n} for n in names]}))
        if "commits/" in joined:
            return _Completed(json.dumps({
                "sha": SHA,
                "commit": {"committer": {"date": "2020-01-01T00:00:00Z"}}}))
        if argv[:3] == ["gh", "run", "list"]:
            return _Completed(json.dumps([{
                "workflowName": "tests", "headSha": SHA,
                "databaseId": RUN_ID, "status": "completed",
                "conclusion": "success", "event": "push",
                "createdAt": "2020-01-01T00:01:00Z",
                "attempt": self.attempt}]))
        if argv[:3] == ["gh", "run", "view"]:
            return _Completed(json.dumps({"jobs": [
                {"name": n, "status": "completed", "conclusion": "success",
                 "databaseId": 1} for n in self.job_names]}))
        return _Completed("{}")


def _render(monkeypatch, capsys, gh: _Gh) -> str:
    monkeypatch.setattr(branch.subprocess, "run", gh)
    monkeypatch.setattr(branch._declared_legs.subprocess, "run", gh)
    monkeypatch.setattr(sys, "argv", ["branch.py", "master"])
    branch.main()
    return capsys.readouterr().out


def _line(out: str, prefix: str) -> str:
    for line in out.splitlines():
        if line.startswith(prefix):
            return line
    raise AssertionError(f"no {prefix!r} line in output:\n{out}")


# ---------------------------------------------------------------------------
# a green it cannot reconcile is not a green
# ---------------------------------------------------------------------------

def test_a_short_job_list_is_not_reported_green(monkeypatch, capsys) -> None:
    """9 legs read, 14 declared, all nine passing — the dangerous shape."""
    out = _render(monkeypatch, capsys, _Gh(MATRIX[:9], MATRIX))

    assert branch.GREEN not in _line(out, "Branch master:"), (
        f"an unreconciled tally still rendered GREEN:\n{out}")
    assert checks.INCOMPLETE_MARK in out, (
        f"9 of 14 legs read with no incompleteness marker:\n{out}")
    assert "9 of 14" in out, f"the two counts are not both stated:\n{out}"


def test_the_absent_legs_are_named(monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, _Gh(MATRIX[:9], MATRIX))
    assert MATRIX[9] in out, f"a leg absent from the tally is unnamed:\n{out}"


def test_the_leg_count_is_never_padded_to_the_declared_one(
        monkeypatch, capsys) -> None:
    """Disclosure, not correction — the op cannot invent the missing legs."""
    out = _render(monkeypatch, capsys, _Gh(MATRIX[:9], MATRIX))
    legs = _line(out, "Legs:")
    assert "9 total" in legs, f"the read count was rewritten:\n{legs}"
    assert "14 total" not in legs, f"invented legs into the tally:\n{legs}"


def test_an_unreadable_second_source_declines_and_is_not_green(
        monkeypatch, capsys) -> None:
    """Could-not-verify is not could-not-pass, and it is not a pass either."""
    out = _render(monkeypatch, capsys, _Gh(MATRIX, MATRIX, api_rc=1))

    assert checks.UNVERIFIED_MARK in out, (
        f"a failed second source rendered as a reconciled tally:\n{out}")
    assert branch.GREEN not in _line(out, "Branch master:"), (
        f"an unverified tally rendered GREEN:\n{out}")
    assert checks.INCOMPLETE_MARK not in out, (
        f"claimed a shortfall it never established:\n{out}")


# ---------------------------------------------------------------------------
# silence when reconciled — a marker that always fires says nothing
# ---------------------------------------------------------------------------

def test_a_reconciled_run_stays_green_and_silent(monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, _Gh(MATRIX, MATRIX))

    assert branch.GREEN in _line(out, "Branch master:"), (
        f"a fully reconciled green was downgraded:\n{out}")
    assert checks.INCOMPLETE_MARK not in out, f"false alarm:\n{out}"
    assert checks.UNVERIFIED_MARK not in out, f"false alarm:\n{out}"


def test_extra_legs_are_not_a_shortfall(monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, _Gh(MATRIX + ["extra"], MATRIX))
    assert checks.INCOMPLETE_MARK not in out, f"false alarm:\n{out}"
    assert branch.GREEN in _line(out, "Branch master:"), out


# ---------------------------------------------------------------------------
# cost
# ---------------------------------------------------------------------------

def test_a_first_attempt_costs_no_extra_call(monkeypatch, capsys) -> None:
    """On attempt 1 `filter=all` is `filter=latest` — nothing to buy."""
    gh = _Gh(MATRIX[:9], MATRIX, attempt=1)
    out = _render(monkeypatch, capsys, gh)

    assert gh.declared_calls == [], (
        f"paid for a second source that cannot differ: {gh.declared_calls}")
    assert checks.INCOMPLETE_MARK not in out, f"false alarm:\n{out}"
    assert checks.UNVERIFIED_MARK not in out, f"false alarm:\n{out}"


def test_a_re_run_costs_one_call_per_reconciled_run(
        monkeypatch, capsys) -> None:
    gh = _Gh(MATRIX[:9], MATRIX, attempt=2)
    _render(monkeypatch, capsys, gh)
    assert len(gh.declared_calls) == 1, gh.declared_calls
    assert f"runs/{RUN_ID}/jobs" in " ".join(gh.declared_calls[0]), (
        gh.declared_calls[0])


def test_the_attempt_field_is_actually_listed(monkeypatch, capsys) -> None:
    """Without it every workflow looks like a re-run and pays for one."""
    seen: list[list[str]] = []
    gh = _Gh(MATRIX, MATRIX, attempt=1)
    inner = gh.__call__

    def spy(argv, *a, **kw):
        seen.append(list(argv))
        return inner(argv, *a, **kw)

    monkeypatch.setattr(branch.subprocess, "run", spy)
    monkeypatch.setattr(branch._declared_legs.subprocess, "run", spy)
    monkeypatch.setattr(sys, "argv", ["branch.py", "master"])
    branch.main()
    capsys.readouterr()

    listing = [a for a in seen if a[:3] == ["gh", "run", "list"]]
    assert listing, f"no run list call: {seen}"
    assert any("attempt" in tok for tok in listing[0]), (
        f"`attempt` not among the listed fields: {listing[0]}")
