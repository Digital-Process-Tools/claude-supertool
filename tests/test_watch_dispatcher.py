"""Unit tests for presets/watch/dispatcher.py arg parsing + listing."""
from __future__ import annotations

import importlib.util
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

WATCH_DIR = Path(__file__).parent.parent / "presets" / "watch"
sys.path.insert(0, str(WATCH_DIR))

_d_spec = importlib.util.spec_from_file_location("watch_dispatcher", WATCH_DIR / "dispatcher.py")
assert _d_spec is not None and _d_spec.loader is not None
dispatcher = importlib.util.module_from_spec(_d_spec)
_d_spec.loader.exec_module(dispatcher)

_t_spec = importlib.util.spec_from_file_location("watch_transport", WATCH_DIR / "transport.py")
assert _t_spec is not None and _t_spec.loader is not None
transport = importlib.util.module_from_spec(_t_spec)
_t_spec.loader.exec_module(transport)


def test_parse_args_basic() -> None:
    assert dispatcher._parse_args(["gitlab-mr", "21803"]) == ("gitlab-mr", "21803", [])


def test_parse_args_with_only_filter() -> None:
    source, watcher_id, only = dispatcher._parse_args(
        ["gitlab-mr", "21803", "only=pipeline_failed,merged"]
    )
    assert source == "gitlab-mr"
    assert watcher_id == "21803"
    assert only == ["pipeline_failed", "merged"]


def test_parse_args_empty_only_ignored() -> None:
    _, _, only = dispatcher._parse_args(["gitlab-mr", "21803", "only="])
    assert only == []


def test_parse_args_missing_id_errors() -> None:
    with pytest.raises(ValueError):
        dispatcher._parse_args(["gitlab-mr"])


def test_parse_args_empty_errors() -> None:
    with pytest.raises(ValueError):
        dispatcher._parse_args([])


def test_parse_args_rejects_double_underscore_in_source() -> None:
    with pytest.raises(ValueError, match="must not contain '__'"):
        dispatcher._parse_args(["bad__source", "21803"])


def test_parse_args_rejects_double_underscore_in_id() -> None:
    with pytest.raises(ValueError, match="must not contain '__'"):
        dispatcher._parse_args(["gitlab-mr", "bad__id"])


def test_parse_args_rejects_a_slash_in_source() -> None:
    with pytest.raises(ValueError, match="must not contain '/'"):
        dispatcher._parse_args(["../../etc", "21803"])


def test_parse_args_rejects_a_slash_in_id() -> None:
    """Feed sources take a filter string as their id, and it lands in a path."""
    with pytest.raises(ValueError, match="must not contain '/'"):
        dispatcher._parse_args(["gitlab-mr-feed", "author=@me,path=a/b"])


def test_load_source_known() -> None:
    mod = dispatcher._load_source("gitlab-mr")
    assert mod is not None
    assert hasattr(mod, "poll")
    assert hasattr(mod, "INTERVAL")


def test_load_source_unknown() -> None:
    assert dispatcher._load_source("does-not-exist") is None


def test_list_empty(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path))
    rows = transport.list_active_pids()
    assert rows == []


def test_list_skips_stale_pid_file(monkeypatch, tmp_path) -> None:
    """Was skipped on Windows because `_pid_alive` raised WinError 87 for a
    PID that cannot exist. It answers on every platform now, so stale-pruning
    is finally exercised on Windows too."""
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path))
    # PID 1 exists (init); a clearly stale impossible PID — pick a very high one
    stale = tmp_path / "supertool-watch-fake__stale.pid"
    stale.write_text("9999999\n")
    rows = transport.list_active_pids()
    assert rows == []
    # Stale file should have been pruned
    assert not stale.exists()


def test_list_includes_live_pid(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path))
    own_pid = os.getpid()
    live = tmp_path / f"supertool-watch-test-source__myid.pid"
    live.write_text(f"{own_pid}\n")
    rows = transport.list_active_pids()
    assert len(rows) == 1
    assert rows[0]["source"] == "test-source"
    assert rows[0]["id"] == "myid"
    assert rows[0]["pid"] == own_pid


# ---------------------------------------------------------------------------
# terminal state cleanup (issue #417 item 4)
# ---------------------------------------------------------------------------

def test_clear_state_removes_the_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path))
    path = Path(transport.state_path("gitlab-mr", "33136"))
    path.write_text("{}")
    assert transport.clear_state("gitlab-mr", "33136") is True
    assert not path.exists()


def test_clear_state_is_false_when_absent(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path))
    assert transport.clear_state("gitlab-mr", "nope") is False


class _StubPoller:
    """Reaches a terminal state on the first tick."""
    INTERVAL = 1

    @staticmethod
    def poll(state, ctx):
        return [], {"mr_state": "merged"}

    @staticmethod
    def is_terminal(state):
        return state.get("mr_state") == "merged"


class _OpenPoller(_StubPoller):
    """Never terminal — bails out on the second tick the way a SIGTERM would."""
    _ticks: list[int] = []

    @staticmethod
    def poll(state, ctx):
        _OpenPoller._ticks.append(1)
        if len(_OpenPoller._ticks) > 1:
            raise SystemExit(0)
        return [], {"mr_state": "opened"}

    @staticmethod
    def is_terminal(state):
        return False


class _EmittingPoller(_StubPoller):
    """Emits one event per tick, then bails out the way a SIGTERM would."""
    _ticks: list[int] = []

    @staticmethod
    def poll(state, ctx):
        _EmittingPoller._ticks.append(1)
        n = len(_EmittingPoller._ticks)
        if n > 2:
            raise SystemExit(0)
        return ([{"event": "pipeline_succeeded", "payload": {"tick": n}}],
                {"mr_state": "opened", "tick": n})

    @staticmethod
    def is_terminal(state):
        return False


#: How long a forked child gets to exit before it is killed and reported.
#:
#: Not a performance budget. Every child here runs a stubbed poller and exits in
#: milliseconds, so a value this large can only ever be reached by a child that
#: is not going to finish at all — the #702 trade, the same one the two
#: `timeout-minutes` figures in `tests.yml` take.
#:
#: It exists because `os.waitpid(pid, 0)` has none, and this fork happens inside
#: an xdist worker: a multi-threaded process, which is a documented deadlock
#: hazard for the child (CPython raises `DeprecationWarning: This process is
#: multi-threaded, use of fork() may lead to deadlocks in the child`). A wedged
#: child does not merely fail its own test. It holds the descriptors it
#: inherited from the worker, including the execnet channel back to the xdist
#: controller, so the controller never sees the worker close and the entire run
#: stops with no test named and no failure reported (#914).
_CHILD_BUDGET_S = 30.0


def _reap(pid: int, budget: float) -> "int | None":
    """Wait for `pid` up to `budget`; kill it and return None past that."""
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done == pid:
            return status
        time.sleep(0.01)
    os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)
    return None


def _run_loop_in_child(monkeypatch, tmp_path, poller, only=None,
                       budget: float = _CHILD_BUDGET_S) -> None:
    """Run _run_poll_loop in a forked child.

    The loop redirects fds 0/1/2 to /dev/null, so it must not run in the
    pytest process. Fork inherits the monkeypatched modules.
    """
    monkeypatch.setattr(dispatcher.transport, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(dispatcher, "_load_source", lambda name: poller)
    pid = os.fork()
    if pid == 0:
        try:
            dispatcher._run_poll_loop("gitlab-mr", "33136", only or [])
        finally:
            os._exit(0)
    status = _reap(pid, budget)
    assert status is not None, (
        f"the forked child {pid} did not exit within {budget}s and was "
        f"killed. A fork from a multi-threaded process inherits every lock in "
        f"whatever state it was in, owned by threads the child does not have, so "
        f"the usual cause is a deadlock on one of them rather than a slow poll."
    )
    assert os.WIFEXITED(status)


#: A lock held, at fork time, by a thread that will not exist in the child.
_FORK_HAZARD_LOCK = threading.Lock()


class _LockTakingPoller(_StubPoller):
    """Takes a lock another thread was holding when the fork happened.

    `os.fork()` copies the memory of the calling thread only. A lock some other
    thread held at that instant is inherited *locked*, by an owner the child
    does not have, so nothing can ever release it — which is the whole content
    of CPython's `DeprecationWarning: This process is multi-threaded, use of
    fork() may lead to deadlocks in the child`.
    """

    @staticmethod
    def poll(state, ctx):
        with _FORK_HAZARD_LOCK:
            pass
        raise SystemExit(0)

    @staticmethod
    def is_terminal(state):
        return False


@pytest.mark.skipif(sys.platform == "win32", reason="requires os.fork")
@pytest.mark.timeout(60)
def test_a_child_that_deadlocks_is_reported_rather_than_waited_on(
        monkeypatch, tmp_path) -> None:
    """A wedged child must end this test, not the whole run.

    `os.waitpid(pid, 0)` has no deadline, and this fork happens inside an xdist
    worker — a process with execnet's receiver thread in it, plus coverage's,
    plus whatever the tests before this one left running. When the child wedges,
    the parent parks in `waitpid` forever *and the child keeps the worker's
    inherited channel descriptors open*, so the controller never sees the worker
    close either. The run stops emitting progress and no test is ever named:
    exactly the `coverage` job's 20-minute cancellations in #914.

    pytest-timeout cannot rescue that. It fires per test item, and by the time
    the controller is the thing that is stuck there is no item left to fail.
    So the deadline belongs here, next to the fork that needs it.
    """
    parked = threading.Event()
    holder = threading.Thread(
        target=lambda: (_FORK_HAZARD_LOCK.acquire(), parked.set(), None),
        daemon=True,
    )
    holder.start()
    assert parked.wait(5), "the fixture never took the lock — nothing would deadlock"

    with pytest.raises(AssertionError, match="did not exit"):
        # A budget this test can wait out, through the argv every other test
        # uses. Appending its own guard instead would let it supply the very
        # deadline it exists to check for.
        _run_loop_in_child(monkeypatch, tmp_path, _LockTakingPoller, budget=3.0)


@pytest.mark.skipif(sys.platform == "win32", reason="requires os.fork")
def test_terminal_poller_removes_its_state_file(monkeypatch, tmp_path) -> None:
    state = tmp_path / "supertool-watch-gitlab-mr__33136.state.json"
    state.write_text('{"source_state": {"mr_state": "opened"}}')
    _run_loop_in_child(monkeypatch, tmp_path, _StubPoller)
    assert not state.exists()
    assert not (tmp_path / "supertool-watch-gitlab-mr__33136.pid").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="requires os.fork")
def test_non_terminal_poller_keeps_its_state_file(monkeypatch, tmp_path) -> None:
    """Only a terminal stop clears the cache — a SIGTERM'd watcher keeps it."""
    state = tmp_path / "supertool-watch-gitlab-mr__33136.state.json"
    state.write_text('{"source_state": {"mr_state": "opened"}}')
    _run_loop_in_child(monkeypatch, tmp_path, _OpenPoller)
    assert state.exists()
    assert not (tmp_path / "supertool-watch-gitlab-mr__33136.pid").exists()


# ---------------------------------------------------------------------------
# the watcher's own filter is part of its published state (issue #434)
#
# `only=` decides which events a poller will ever emit, and it lived only in
# the process's memory. Another tier asking "will anyone report this merge?"
# could see the poller was alive and had no way to learn it was filtered away
# from saying so — so the answer had to be a guess, in a place where guessing
# wrong means an event nobody reports.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="requires os.fork")
def test_a_poller_publishes_its_event_filter(monkeypatch, tmp_path) -> None:
    state = tmp_path / "supertool-watch-gitlab-mr__33136.state.json"
    _run_loop_in_child(monkeypatch, tmp_path, _OpenPoller,
                       only=["pipeline_failed", "merged"])
    assert json.loads(state.read_text(encoding="utf-8"))["only"] == ["pipeline_failed", "merged"]


@pytest.mark.skipif(sys.platform == "win32", reason="requires os.fork")
def test_an_unfiltered_poller_publishes_an_empty_filter(monkeypatch, tmp_path) -> None:
    """`[]` is the recorded answer "emits everything", and it has to be
    distinguishable from the key being absent, which means "we do not know"."""
    state = tmp_path / "supertool-watch-gitlab-mr__33136.state.json"
    _run_loop_in_child(monkeypatch, tmp_path, _OpenPoller)
    assert json.loads(state.read_text(encoding="utf-8"))["only"] == []


@pytest.mark.skipif(sys.platform == "win32", reason="requires os.fork")
def test_the_filter_survives_the_polls_that_rewrite_the_state(monkeypatch, tmp_path) -> None:
    """The loop read-modify-writes the state every tick. A filter clobbered on
    tick two is a filter that answers correctly exactly once."""
    state = tmp_path / "supertool-watch-gitlab-mr__33136.state.json"
    _OpenPoller._ticks.clear()
    _run_loop_in_child(monkeypatch, tmp_path, _OpenPoller, only=["merged"])
    body = json.loads(state.read_text(encoding="utf-8"))
    assert body["only"] == ["merged"]
    assert body["source_state"] == {"mr_state": "opened"}


# ---------------------------------------------------------------------------
# #464 — spawning watchers for MRs whose state had not moved in a week emitted
# `pipeline_succeeded` for week-old pipelines, shaped exactly like news. The
# emission is wanted (it is how a new watcher reports what it found); what was
# missing is any way for the consumer to tell the two apart.
# ---------------------------------------------------------------------------

def _capture_emissions(monkeypatch, tmp_path):
    """Log every record the real emit_event builds. The loop runs in a forked
    child, so the log has to be a file rather than a list."""
    log = tmp_path / "emitted.jsonl"

    def _capture(record):
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    monkeypatch.setattr(dispatcher.transport, "emit_socket", _capture)
    return log


def _read_emissions(log):
    if not log.exists():
        return []
    return [json.loads(line) for line in
            log.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.skipif(sys.platform == "win32", reason="requires os.fork")
def test_the_first_polls_events_are_marked_first_tick(monkeypatch, tmp_path) -> None:
    _EmittingPoller._ticks.clear()
    log = _capture_emissions(monkeypatch, tmp_path)
    _run_loop_in_child(monkeypatch, tmp_path, _EmittingPoller)
    records = _read_emissions(log)
    assert records, "the stub emits on every tick — an empty log means the loop never ran"
    assert records[0]["first_tick"] is True
    assert records[0]["payload"]["tick"] == 1


@pytest.mark.skipif(sys.platform == "win32", reason="requires os.fork")
def test_a_later_polls_events_are_not_marked_first_tick(monkeypatch, tmp_path) -> None:
    """The false-positive direction. A marker that never turns off is decoration."""
    _EmittingPoller._ticks.clear()
    log = _capture_emissions(monkeypatch, tmp_path)
    _run_loop_in_child(monkeypatch, tmp_path, _EmittingPoller)
    records = _read_emissions(log)
    assert len(records) == 2
    assert records[1]["first_tick"] is False
    assert records[1]["payload"]["tick"] == 2


@pytest.mark.skipif(sys.platform == "win32", reason="requires os.fork")
def test_a_watcher_resuming_from_saved_state_has_no_first_tick(monkeypatch, tmp_path) -> None:
    """First tick means "this watcher knew nothing", not "this process is
    young" — a restarted poller carrying its state forward is not bootstrapping."""
    _EmittingPoller._ticks.clear()
    state = tmp_path / "supertool-watch-gitlab-mr__33136.state.json"
    state.write_text(json.dumps({"source_state": {"mr_state": "opened", "tick": 0}}),
                     encoding="utf-8")
    log = _capture_emissions(monkeypatch, tmp_path)
    _run_loop_in_child(monkeypatch, tmp_path, _EmittingPoller)
    records = _read_emissions(log)
    assert records
    assert all(r["first_tick"] is False for r in records)


@pytest.mark.skipif(sys.platform == "win32", reason="requires os.fork")
def test_a_successful_poll_clears_a_previous_error(monkeypatch, tmp_path) -> None:
    """`radar` reports a poller's last_error as a current fact, so a message
    that outlived the failure would be a permanent false alarm."""
    state = tmp_path / "supertool-watch-gitlab-mr__33136.state.json"
    state.write_text(json.dumps({
        "source_state": {"mr_state": "opened"},
        "last_error": {"ts": "2026-07-27T16:00:00Z", "message": "401 Unauthorized"},
    }))
    _run_loop_in_child(monkeypatch, tmp_path, _OpenPoller)
    assert "last_error" not in json.loads(state.read_text(encoding="utf-8"))
