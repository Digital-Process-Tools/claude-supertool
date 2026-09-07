"""`presets/_pr_board.py` -- the open-PR board fetch and the default-branch
head/run read, shared by radar's GitHub tier and the dashboard op (#958).

Before this file, `presets/watch/tiers/gh_prs.py` (`live_open_prs`) and
`presets/dashboard/dashboard.py` (`collect_board`/`collect_default`) each ran
their own copy of "spawn `gh pr list`, parse the JSON" and "read the default
branch's head commit and run list" -- the mechanical fetch layer underneath
two callers that then diverge on purpose (a delta view vs a state view, and
radar's poller machinery vs the dashboard's declared-workflow scoping). This
pins the three-state contract the shared fetch owes both: a board that could
not be fetched must not render as an empty one, and a default-branch read
that failed must not render as though nothing was asked.

Every "must report unknown" case is paired with a "must report the real
answer" case in the same fixture -- the bar from the issue: would this test
still pass if the extraction did nothing? A module that only ever returned
the failure branch would pass every negative case here and fail every
positive one.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "presets"))

import _pr_board  # noqa: E402


class _Result:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


# ---------------------------------------------------------------------------
# run_pr_list -- the open-PR board fetch
# ---------------------------------------------------------------------------

def test_run_pr_list_returns_the_real_board_on_success(monkeypatch):
    payload = [{"number": 1}, {"number": 2}]
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: _Result(json.dumps(payload), "", 0))
    data, err, rc, _raw = _pr_board.run_pr_list(["gh", "pr", "list"])
    assert data == payload
    assert err == ""
    assert rc == 0


def test_run_pr_list_an_empty_board_is_not_an_error(monkeypatch):
    """A filter that matched nothing is real data, not a failure."""
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: _Result("[]", "", 0))
    data, err, rc, _raw = _pr_board.run_pr_list(["gh", "pr", "list"])
    assert data == []
    assert err == ""


def test_run_pr_list_nonzero_exit_is_none_not_empty(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _Result("", "error: not logged in to any GitHub hosts", 1))
    data, err, rc, _raw = _pr_board.run_pr_list(["gh", "pr", "list"])
    assert data is None, "a board GitHub refused must never render as an empty one"
    assert "not logged in" in err
    assert rc == 1


def test_run_pr_list_spawn_failure_is_none_with_no_returncode(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("gh")
    monkeypatch.setattr(subprocess, "run", boom)
    data, err, rc, _raw = _pr_board.run_pr_list(["gh", "pr", "list"])
    assert data is None
    assert err
    assert rc is None, "the process never finished, so there is no exit code to report"


def test_run_pr_list_timeout_is_none_with_no_returncode(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="gh", timeout=30)
    monkeypatch.setattr(subprocess, "run", boom)
    data, err, rc, _raw = _pr_board.run_pr_list(["gh", "pr", "list"], timeout=30)
    assert data is None
    assert "timed out" in err.lower() or "timeout" in err.lower()
    assert rc is None


def test_run_pr_list_unparseable_json_is_none(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: _Result("not json", "", 0))
    data, err, rc, _raw = _pr_board.run_pr_list(["gh", "pr", "list"])
    assert data is None
    assert "JSON" in err or "json" in err
    assert rc == 0


def test_run_pr_list_non_list_json_is_none(monkeypatch):
    """`gh pr list --json` answering with an object, not a list, is a failure
    to fetch a board, not a board of one row."""
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: _Result(json.dumps({"oops": 1}), "", 0))
    data, err, rc, _raw = _pr_board.run_pr_list(["gh", "pr", "list"])
    assert data is None
    assert "list" in err.lower()


# ---------------------------------------------------------------------------
# head_and_runs -- the default-branch health fetch
# ---------------------------------------------------------------------------

class _FakeBranch:
    def __init__(self, head=("c" * 40, 120, ""), runs=([{"id": 1}], "")):
        self._head = head
        self._runs = runs
        self.run_list_calls = []

    def _head_commit(self, ref):
        return self._head

    def _run_list(self, ref):
        self.run_list_calls.append(ref)
        return self._runs


def test_head_and_runs_returns_the_real_read_on_success():
    branch = _FakeBranch(head=("a" * 40, 60, ""), runs=([{"id": 1}], ""))
    sha, age, runs, err = _pr_board.head_and_runs(branch, "master")
    assert sha == "a" * 40
    assert age == 60
    assert runs == [{"id": 1}]
    assert err == ""


def test_head_and_runs_no_runs_yet_is_not_an_error():
    """An empty run list is a real, established fact (#615) -- not unknown."""
    branch = _FakeBranch(runs=([], ""))
    sha, age, runs, err = _pr_board.head_and_runs(branch, "master")
    assert runs == []
    assert err == ""


def test_head_and_runs_a_failed_head_commit_is_unknown_not_empty():
    branch = _FakeBranch(head=("", None, "ERROR: gh timed out resolving ref"))
    sha, age, runs, err = _pr_board.head_and_runs(branch, "master")
    assert runs is None, "a head read that failed must not render as no runs"
    assert "timed out" in err
    assert branch.run_list_calls == [], "the run list must not be asked for over an unresolved head"


def test_head_and_runs_a_failed_run_list_is_unknown_not_empty():
    branch = _FakeBranch(head=("b" * 40, 10, ""),
                         runs=(None, "ERROR: gh timed out listing runs"))
    sha, age, runs, err = _pr_board.head_and_runs(branch, "master")
    assert sha == "b" * 40, "the head that WAS established should still be reported"
    assert runs is None
    assert "timed out" in err


def test_head_and_runs_none_runs_with_no_error_is_still_unknown():
    """Defensive: a caller that returns `(None, "")` must not read as `[]`."""
    branch = _FakeBranch(head=("d" * 40, 1, ""), runs=(None, ""))
    sha, age, runs, err = _pr_board.head_and_runs(branch, "master")
    assert runs is None
    assert err, "an unreadable run list must say so even if the caller forgot to"
