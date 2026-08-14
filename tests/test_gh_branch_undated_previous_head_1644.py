"""`gh-branch` — a run it could not date is disclosed, never silently dropped (#1644).

#1618 replaced list position with `createdAt` as the previous-head ordering. The
fallback to position survived for the case where *no* candidate parses, and the
partial case — some parse, some do not — drops the unparseable ones from the
ranking with no sentence anywhere. Both leave the reader unable to tell "ordered
by time" from "ordered by whatever the API returned", which is the property
#1618 existed to remove.

`_created` accepts exactly ``%Y-%m-%dT%H:%M:%SZ``. ``2026-08-13T21:52:04+00:00``
is the same instant in a legal RFC3339 spelling and parses to ``None``, so the
*newest* run can be the one dropped and a 2020 run can win. GitHub emits the `Z`
form on this repo today — the point is not that it is broken, it is that nothing
says so when it stops being true.

The fix is disclosure, not a wider parser: widening `strptime` makes the
assumption invisible again rather than declared.
"""
from __future__ import annotations

import importlib.util
import json
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


branch = _load("presets/github/branch.py", "github_branch_1644")

_HEAD = "339d04db5afc35ee44470677b996b8364e8ad20b"
_PREV = "60ec9b17f0e4b1d9c4a5e6f70819293a4b5c6d7e"
_ANCIENT = "d7e67ee3452bd8efcec80a0eec788917b3c9e6cb"

#: A legal RFC3339 spelling of the same instant that `_created` cannot read.
_OFFSET_FORM = "2026-08-13T21:52:04+00:00"


class _Completed:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _run(workflow: str, sha: str, run_id: int, created: str) -> dict:
    return {"workflowName": workflow, "headSha": sha, "databaseId": run_id,
            "status": "completed", "conclusion": "success", "event": "push",
            "createdAt": created, "attempt": 1}


def _iso(secs_ago: int) -> str:
    return (datetime.now(timezone.utc)
            - timedelta(seconds=secs_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _render(monkeypatch, capsys, *, runs: list, jobs: dict, head_age: int) -> str:
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
            payload = jobs.get(int(argv_[3]))
            if payload is None:
                return _Completed("", 1, "HTTP 500")
            return _Completed(json.dumps({"jobs": payload}))
        return _Completed("", 1, "unexpected call")

    monkeypatch.setattr(branch.subprocess, "run", fake)
    monkeypatch.setattr(sys, "argv", ["branch.py", "master"])
    branch.main()
    return capsys.readouterr().out


# ---------------------------------------------------------------------------
# the basis is a fact the op holds and must be able to state
# ---------------------------------------------------------------------------

def test_an_offset_spelling_is_undated_not_merely_older() -> None:
    """The premise. If this ever parses, the rest of this file is moot."""
    assert branch._created({"createdAt": _OFFSET_FORM}) is None
    assert branch._created({"createdAt": "2026-08-13T21:52:04Z"}) is not None


def test_a_ranking_that_dropped_a_candidate_says_how_many_it_dropped() -> None:
    """The newest run is the one dropped, and a 2020 run wins the ranking.

    `previous_head` still answers — the ranking over what could be dated is the
    best available — but the answer is qualified, and the count is what makes it
    checkable rather than a general hedge.
    """
    runs = [_run("ci", _PREV, 31720069489, _OFFSET_FORM),
            _run("old", _ANCIENT, 30287882010, "2020-01-01T00:00:00Z")]

    prev, _names = branch.previous_head(runs, _HEAD)
    assert prev == _ANCIENT, (
        "premise moved: the undated newest run is expected to lose the "
        f"ranking, got {prev[:7]}")

    basis, undated, candidates = branch.previous_head_basis(runs, _HEAD)
    assert basis == "time"
    assert (undated, candidates) == (1, 2)

    lines = branch.previous_head_lines(runs, _HEAD, prev)
    assert lines, "a dropped candidate produced no disclosure at all"
    body = " ".join(lines)
    assert "1 of 2" in body, body
    assert "createdAt" in body, body


def test_a_ranking_with_nothing_datable_says_it_used_list_position() -> None:
    """The exact behaviour #1618 removed, still reachable and now declared."""
    runs = [_run("ci", _PREV, 31720069489, _OFFSET_FORM),
            _run("old", _ANCIENT, 30287882010, "")]

    prev, _names = branch.previous_head(runs, _HEAD)
    assert prev == _PREV, "the position fallback was deleted, not disclosed"

    basis, undated, candidates = branch.previous_head_basis(runs, _HEAD)
    assert basis == "position"
    assert (undated, candidates) == (2, 2)

    body = " ".join(branch.previous_head_lines(runs, _HEAD, prev))
    assert "list position" in body.lower(), body
    assert "1618" in body, body


def test_a_fully_datable_listing_says_nothing() -> None:
    """A hedge printed on every ordinary call is one that gets tuned out.

    Asserted against the same shape as the two above, so what separates silence
    from the sentence is the timestamps and nothing else.
    """
    runs = [_run("ci", _PREV, 31720069489, "2026-08-13T21:52:04Z"),
            _run("old", _ANCIENT, 30287882010, "2020-01-01T00:00:00Z")]

    basis, undated, candidates = branch.previous_head_basis(runs, _HEAD)
    assert (basis, undated, candidates) == ("time", 0, 2)
    assert branch.previous_head_lines(runs, _HEAD, _PREV) == []


def test_no_candidate_at_all_is_a_third_answer_not_a_disclosure() -> None:
    """Every run is on the head: there is no previous head to qualify."""
    runs = [_run("ci", _HEAD, 1, "")]
    assert branch.previous_head_basis(runs, _HEAD) == ("none", 0, 0)
    assert branch.previous_head_lines(runs, _HEAD, "") == []


# ---------------------------------------------------------------------------
# and it reaches the reader
# ---------------------------------------------------------------------------

def test_the_render_discloses_a_position_ranking(monkeypatch, capsys) -> None:
    """The op itself must carry it — a helper nobody prints is the same
    silence in a different place."""
    runs = [_run("tests", _PREV, 31720069489, _OFFSET_FORM),
            _run("CodeQL", _HEAD, 9001, _OFFSET_FORM)]
    jobs = {9001: [{"name": "Analyze", "status": "completed",
                    "conclusion": "success", "databaseId": 7, "steps": []}]}

    out = _render(monkeypatch, capsys, runs=runs, jobs=jobs, head_age=48 * 3600)

    assert "list position" in out.lower(), (
        "the previous head was ranked by position and the render did not "
        "say so:" + out)


def test_the_render_stays_quiet_when_everything_parsed(
        monkeypatch, capsys) -> None:
    runs = [_run("tests", _PREV, 31720069489, _iso(700)),
            _run("CodeQL", _HEAD, 9001, _iso(600))]
    jobs = {9001: [{"name": "Analyze", "status": "completed",
                    "conclusion": "success", "databaseId": 7, "steps": []}]}

    out = _render(monkeypatch, capsys, runs=runs, jobs=jobs, head_age=48 * 3600)

    assert "list position" not in out.lower(), out
    assert "could not be dated" not in out.lower(), out
