"""`gh-branch` — an empty run listing is not evidence a workflow did not run (#1618).

Hit live at the v0.41.0 release gate. GitHub created two runs on master's head
``339d04d`` at 16:19:44Z. At 16:31Z, twelve minutes later, ``gh-branch:master``
printed::

    Verdict: NO RUN - zero workflow runs on 339d04d; the head commit is 11m old ...
    Workflows on the previous head d7e67ee with no run on 339d04d:
      tests - did NOT run on this commit. ...

and at 16:34Z, on the same unchanged head, it printed both run ids. Nothing was
created in between: ``gh run list --branch master --limit 60`` simply did not
return them on the middle call.

Two separable defects, both pinned here.

*The previous head was named from list position.* ``previous_head`` took the
first entry whose sha differed, which is the newest one only if GitHub returns
the list newest-first. The incident named ``d7e67ee`` - a commit from
2026-07-27, 527 commits and 17 days behind the head, carrying exactly one run.
A correct newest-60 window on that branch cannot contain it, so the returned
order was not the assumed one. The docstring already promised "the newest run
set that is not this SHA"; the code did not implement it.

*The tool's absence was printed as the world's.* ``did NOT run on this commit``
is a flat assertion resting on a listing that the line above it had just shown
did not contain the head's runs at all. At a release gate the two readings take
opposite actions: a genuine absence means the commit is uncovered and the tag
should not be cut, a lagging listing means wait and re-ask. This is the house
defect (``docs/validators.md`` section "Declining instead of guessing") on the
one surface the release procedure quotes.
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


branch = _load("presets/github/branch.py", "github_branch_1618")

# The three commits from the incident, at their real lengths.
_HEAD = "339d04db5afc35ee44470677b996b8364e8ad20b"
_PREV = "60ec9b17f0e4b1d9c4a5e6f70819293a4b5c6d7e"
_ANCIENT = "d7e67ee3452bd8efcec80a0eec788917b3c9e6cb"


class _Completed:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _run(workflow: str, sha: str, status: str, conclusion: str | None,
         run_id: int, created: str = "2026-08-13T16:19:18Z",
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
            head_age: int) -> str:
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


def _state(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("Branch "):
            m = re.match(r"Branch .+?: (.+)$", line)
            assert m, f"unparseable branch line: {line}"
            return m.group(1).strip()
    raise AssertionError("no Branch line in output:\n" + out)


# ---------------------------------------------------------------------------
# the previous head is the newest other run, not the first one listed
# ---------------------------------------------------------------------------

def test_the_previous_head_is_the_newest_other_run_not_the_first_listed() -> None:
    """List order is GitHub's to choose; `newest` is this op's own claim.

    The ancient run is listed first, exactly as the incident's listing had it.
    Reading position instead of time names a 17-day-old commit as "the previous
    head" and then reports its one workflow as missing from the head.
    """
    runs = [_run("tests", _ANCIENT, "completed", "success", 30287882010,
                 created="2026-07-27T17:08:00Z"),
            _run("tests", _PREV, "completed", "success", 31720069489,
                 created="2026-08-13T16:19:18Z"),
            _run("CodeQL", _PREV, "completed", "success", 31720069349,
                 created="2026-08-13T16:19:17Z")]

    prev, names = branch.previous_head(runs, _HEAD)

    assert prev == _PREV, (
        "the previous head was taken from list position, not from createdAt: "
        f"got {prev[:7]}")
    assert names == {"tests", "CodeQL"}


def test_an_unreadable_created_at_falls_back_to_position_not_to_nothing() -> None:
    """No timestamp anywhere is a third state, not a reason to answer nothing.

    Two undated candidates, so the assertion discriminates: an implementation
    that filters undated runs out of the selection answers `("", set())` and
    deletes the only evidence there is for "ran last time, not this time".
    """
    runs = [_run("tests", _PREV, "completed", "success", 31720069489,
                 created=""),
            _run("tests", _ANCIENT, "completed", "success", 30287882010,
                 created="")]
    prev, names = branch.previous_head(runs, _HEAD)
    assert prev == _PREV, (
        "with no timestamp anywhere the previous head is list position, which "
        f"is the first other entry; got {prev[:7] or '<nothing>'}")
    assert names == {"tests"}


# ---------------------------------------------------------------------------
# a listing that returned nothing for the head cannot assert a non-run
# ---------------------------------------------------------------------------

def test_a_listing_with_no_run_on_the_head_does_not_assert_a_non_run(
        monkeypatch, capsys) -> None:
    runs = [_run("tests", _PREV, "completed", "success", 31720069489,
                 created=_iso(700))]

    out = _render(monkeypatch, capsys, runs=runs, jobs={}, head_age=660)

    assert _state(out) == branch.NO_RUN
    assert "did NOT run on this commit" not in out, (
        "an empty listing was printed as an established absence:\n" + out)
    assert "did not see the head commit" in out, (
        "nothing said the listing itself may not have caught up:\n" + out)


def test_the_disclosure_says_how_far_behind_the_listing_is(
        monkeypatch, capsys) -> None:
    """A listing 23s behind and one 17d behind are not the same news."""
    runs = [_run("tests", _ANCIENT, "completed", "success", 30287882010,
                 created=_iso(660 + 17 * 86400))]

    out = _render(monkeypatch, capsys, runs=runs, jobs={}, head_age=660)

    assert "17d" in out, (
        "the gap between the head commit and the newest run returned was not "
        "stated:\n" + out)


def test_a_head_whose_runs_did_come_back_gets_no_lag_disclosure(
        monkeypatch, capsys) -> None:
    runs = [_run("CodeQL", _HEAD, "completed", "success", 9001,
                 created=_iso(600)),
            _run("tests", _PREV, "completed", "success", 9003,
                 created=_iso(700))]
    jobs = {9001: [_job("Analyze", "completed", "success")]}

    out = _render(monkeypatch, capsys, runs=runs, jobs=jobs, head_age=660)

    assert "tests" in out
    assert "did not see the head commit" not in out, (
        "the listing returned this head's runs; nothing was lagging:\n" + out)
    # The render assertion above is satisfied by a build with no disclosure at
    # all, so the gate itself is asserted directly: same runs, same young head,
    # and only the presence of a selection separates silence from the sentence.
    assert branch.stale_listing_lines(runs, {"CodeQL": runs[0]}, _HEAD, 660) == []
    assert branch.stale_listing_lines(runs, {}, _HEAD, 660) != []


def test_past_the_creation_window_the_lag_disclosure_stops(
        monkeypatch, capsys) -> None:
    """A path filter is legitimate. A forever-hedge is a disclosure tuned out."""
    runs = [_run("tests", _PREV, "completed", "success", 9003,
                 created=_iso(48 * 3600))]

    out = _render(monkeypatch, capsys, runs=runs, jobs={}, head_age=48 * 3600)

    assert "tests" in out
    assert "did not see the head commit" not in out, (
        "a two-day-old head is past the window; the listing has caught "
        "up:\n" + out)
    # Same reason as above: only the age separates these two calls, so the
    # window guard is what is being asserted rather than the feature's absence.
    assert branch.stale_listing_lines(runs, {}, _HEAD, 48 * 3600) == []
    assert branch.stale_listing_lines(runs, {}, _HEAD, 660) != []
