#!/usr/bin/env python3
"""#1408 — no cell of the workflow table may assert what the verdict refused.

GitHub can close a run object as ``completed / success`` while one of that run's
own jobs is still ``in_progress``. Observed twice on 2026-08-11 — `pytest
(windows-latest, 3.11)` on run 31501780284 and `pytest (macos-latest, 3.12)` on
31507113066 — and neither leg would ever have reported, because the run object
was already closed.

`gh-branch` read both fields honestly and printed both, so one render said two
things about one workflow on one SHA::

    Verdict: NOT GREEN — ... `tests` has not concluded on 2a15689 ...
    tests    concluded    success    15 total: 14 passed, 0 failed, 1 pending

A reader who resolves that in favour of the table merges a commit the op
refused to clear — the exact read #445/#454 exist to prevent, with the op
supplying the counter-evidence itself.

These tests therefore assert neither half is *right*. They derive the set of
workflows the **verdict** refused straight off the rendered verdict line, and
then require that no row of those workflows states a conclusion plainly. A test
that read only the verdict would pass with the contradiction intact.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


branch = _load("presets/github/branch.py", "github_branch_1408")

_HEAD = "2a15689f2f0e4b1d9c4a5e6f70819293a4b5c6d7"
_PREV = "e34cef6f1e2f3a4b5c6d7e8f90a1b2c3d4e5f607"


class _Completed:
    def __init__(self, stdout: str, returncode: int = 0,
                 stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _run(workflow: str, sha: str, status: str, conclusion: str | None,
         run_id: int, created: str = "2026-08-11T15:00:00Z",
         attempt: int = 1) -> dict:
    return {"workflowName": workflow, "headSha": sha, "databaseId": run_id,
            "status": status, "conclusion": conclusion, "event": "push",
            "createdAt": created, "attempt": attempt}


def _job(name: str, status: str, conclusion: str | None = None) -> dict:
    return {"name": name, "status": status, "conclusion": conclusion,
            "databaseId": 900 + len(name), "steps": []}


def _iso(secs_ago: int) -> str:
    return (datetime.now(timezone.utc)
            - timedelta(seconds=secs_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _render(monkeypatch, capsys, *, runs: list[dict], jobs: dict[int, Any],
            head_age: int = 4000) -> str:
    def fake(cmd, *a, **kw):
        argv_ = list(cmd)
        if argv_[:3] == ["gh", "repo", "view"]:
            return _Completed(json.dumps({
                "nameWithOwner": "Digital-Process-Tools/claude-supertool",
                "defaultBranchRef": {"name": "master"}}))
        if argv_[:2] == ["gh", "api"]:
            return _Completed(json.dumps({
                "sha": _HEAD,
                "commit": {"committer": {"date": _iso(head_age)}}}))
        if argv_[:3] == ["gh", "run", "list"]:
            return _Completed(json.dumps(runs))
        if argv_[:3] == ["gh", "run", "view"]:
            payload = jobs.get(int(argv_[3]), "__missing__")
            if payload == "__missing__":
                return _Completed("", 1, "HTTP 500")
            return _Completed(json.dumps({"jobs": payload}))
        return _Completed("", 1, "unexpected call")

    monkeypatch.setattr(branch.subprocess, "run", fake)
    monkeypatch.setattr(sys, "argv", ["branch.py", "fix/1394"])
    branch.main()
    return capsys.readouterr().out


# ---------------------------------------------------------------------------
# reading the two halves of the render apart from each other
# ---------------------------------------------------------------------------

def _verdict(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("Verdict:"):
            return line
    raise AssertionError("no Verdict line in output:\n" + out)


def _refused_workflows(out: str) -> set[str]:
    """Workflows the verdict line itself says have not concluded.

    Read off the render rather than recomputed, so the test compares the two
    halves a reader sees instead of comparing one of them against a second
    derivation of the same predicate.
    """
    line = _verdict(out)
    if "not concluded" not in line:
        return set()
    head = line.split("not concluded")[0]
    return set(re.findall(r"`([^`]+)`", head))


_TALLY = re.compile(r"(\d+ total:|UNREAD)")


def _rows(out: str) -> list[str]:
    lines = out.splitlines()
    head = None
    for n, line in enumerate(lines):
        if line.startswith("Workflow") and line.rstrip().endswith("Legs"):
            head = n
            break
    if head is None:
        raise AssertionError("no workflow table in output:\n" + out)
    rows = []
    for line in lines[head + 2:]:
        if not line.strip():
            break
        rows.append(line)
    return rows


def _cells(out: str, name: str) -> tuple[str, str]:
    """`(phase, outcome)` for one workflow row, located by position not width."""
    for row in _rows(out):
        if row.startswith(name):
            m = _TALLY.search(row)
            assert m, "no leg tally in row: " + row
            left = row[len(name):m.start()].strip()
            phase, _, outcome = left.partition(" ")
            return phase.strip(), outcome.strip()
    raise AssertionError("no row for " + repr(name) + " in output:\n" + out)


# The payload from #1408: `tests` closed `completed / success` with one leg
# that never concluded, beside a CodeQL that is genuinely done.
_ORPHAN_RUNS = [
    _run("CodeQL", _HEAD, "completed", "success", 3001),
    _run("tests", _HEAD, "completed", "success", 3002),
    _run("CodeQL", _PREV, "completed", "success", 2001),
    _run("tests", _PREV, "completed", "success", 2002),
]

_ORPHAN_JOBS = {
    3001: [_job("Analyze (python)", "completed", "success")],
    3002: ([_job(f"pytest ({i})", "completed", "success") for i in range(14)]
           + [_job("pytest (windows-latest, 3.11)", "in_progress")]),
    2001: [_job("Analyze (python)", "completed", "success")],
    2002: [_job("pytest", "completed", "success")],
}

_ALL_GREEN_JOBS = {
    3001: [_job("Analyze (python)", "completed", "success")],
    3002: [_job(f"pytest ({i})", "completed", "success") for i in range(15)],
    2001: [_job("Analyze (python)", "completed", "success")],
    2002: [_job("pytest", "completed", "success")],
}


# ---------------------------------------------------------------------------
# the centrepiece
# ---------------------------------------------------------------------------

def test_the_verdict_refuses_the_run_whose_leg_never_concluded(
        monkeypatch, capsys) -> None:
    """The half that was already right, pinned so the table cannot buy its

    agreement by weakening the verdict instead.
    """
    out = _render(monkeypatch, capsys, runs=_ORPHAN_RUNS, jobs=_ORPHAN_JOBS)

    assert branch.NOT_GREEN in _verdict(out), out
    assert _refused_workflows(out) == {"tests"}, _verdict(out)


def test_no_row_states_a_conclusion_the_verdict_refused(
        monkeypatch, capsys) -> None:
    """The contradiction itself, read out of one render.

    Before #1408 the `tests` row read `concluded  success` three lines under a
    verdict saying `tests` had not concluded.
    """
    out = _render(monkeypatch, capsys, runs=_ORPHAN_RUNS, jobs=_ORPHAN_JOBS)

    refused = _refused_workflows(out)
    assert refused, "fixture no longer produces a refusal: " + _verdict(out)
    for name in sorted(refused):
        _phase, outcome = _cells(out, name)
        assert outcome not in {"success", "no conclusion"}, (
            "the verdict refused " + repr(name) + ", and its row asserts "
            + repr(outcome) + " anyway — one render, two answers:\n" + out)


def test_the_render_says_which_of_the_two_sources_disagree(
        monkeypatch, capsys) -> None:
    """A marker with no sentence is a second thing to go and look up."""
    out = _render(monkeypatch, capsys, runs=_ORPHAN_RUNS, jobs=_ORPHAN_JOBS)

    explain = [ln for ln in out.splitlines()
               if "tests" in ln and "run" in ln and "leg" in ln
               and "total:" not in ln]
    assert explain, (
        "no line explains that the run object concluded without one of its "
        "own legs:\n" + out)


def test_a_workflow_the_verdict_cleared_still_reads_plainly(
        monkeypatch, capsys) -> None:
    """The over-marking guard: CodeQL is genuinely done in the same render."""
    out = _render(monkeypatch, capsys, runs=_ORPHAN_RUNS, jobs=_ORPHAN_JOBS)

    phase, outcome = _cells(out, "CodeQL")
    assert phase == branch.PHASE_CONCLUDED, out
    assert outcome == "success", (
        "CodeQL concluded with every leg passed, and its row is qualified "
        "anyway (" + repr(outcome) + ") — a marker on every row marks "
        "nothing:\n" + out)


def test_an_all_green_commit_carries_no_marker_anywhere(
        monkeypatch, capsys) -> None:
    """Would still pass if the fix marked every concluded run. This is the

    test that would not.
    """
    out = _render(monkeypatch, capsys,
                  runs=_ORPHAN_RUNS, jobs=_ALL_GREEN_JOBS)

    assert branch.GREEN in _verdict(out), out
    assert _refused_workflows(out) == set()
    for name in ("CodeQL", "tests"):
        assert _cells(out, name) == (branch.PHASE_CONCLUDED, "success"), out


def test_a_still_running_run_is_untouched_by_the_orphan_marker(
        monkeypatch, capsys) -> None:
    """A pending leg under a run that has *not* closed is an ordinary wait."""
    runs = [
        _run("CodeQL", _HEAD, "completed", "success", 3001),
        _run("tests", _HEAD, "in_progress", None, 3002),
        _run("CodeQL", _PREV, "completed", "success", 2001),
        _run("tests", _PREV, "completed", "success", 2002),
    ]
    out = _render(monkeypatch, capsys, runs=runs, jobs=_ORPHAN_JOBS)

    phase, outcome = _cells(out, "tests")
    assert phase == branch.PHASE_RUNNING, out
    assert outcome == "not yet", out
    assert "never concluded" not in out, (
        "a run still moving is not a run that closed without its leg:\n" + out)


def test_the_orphan_sentence_agrees_with_its_own_count(
        monkeypatch, capsys) -> None:
    """#841's rule, one sentence further on.

    The first draft read `1 of its leg never concluded` — a partitive with a
    singular noun, from `_agrees` applied to a phrase that wanted the plural
    whatever the count. Both forms are pinned, because a count-dependent word
    checked at one value is checked at the value the author happened to try.
    """
    two = dict(_ORPHAN_JOBS)
    two[3002] = ([_job(f"pytest ({i})", "completed", "success")
                  for i in range(13)]
                 + [_job("pytest (windows-latest, 3.11)", "in_progress"),
                    _job("pytest (macos-latest, 3.12)", "in_progress")])

    one = _render(monkeypatch, capsys, runs=_ORPHAN_RUNS, jobs=_ORPHAN_JOBS)
    many = _render(monkeypatch, capsys, runs=_ORPHAN_RUNS, jobs=two)

    assert "1 leg of it never concluded" in one, one
    assert "2 legs of it never concluded" in many, many
