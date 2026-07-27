"""Unit tests for presets/watch/dispatcher.py arg parsing + listing."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
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


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="OpenProcess for PID-existence check raises OSError WinError 87 on "
    "Windows for invalid PIDs; the stale-pruning logic uses POSIX "
    "os.kill(pid, 0) semantics that don't map cleanly to Windows.",
)
def test_list_skips_stale_pid_file(monkeypatch, tmp_path) -> None:
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


def _run_loop_in_child(monkeypatch, tmp_path, poller) -> None:
    """Run _run_poll_loop in a forked child.

    The loop redirects fds 0/1/2 to /dev/null, so it must not run in the
    pytest process. Fork inherits the monkeypatched modules.
    """
    monkeypatch.setattr(dispatcher.transport, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(dispatcher, "_load_source", lambda name: poller)
    pid = os.fork()
    if pid == 0:
        try:
            dispatcher._run_poll_loop("gitlab-mr", "33136", [])
        finally:
            os._exit(0)
    _, status = os.waitpid(pid, 0)
    assert os.WIFEXITED(status)


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
    assert "last_error" not in json.loads(state.read_text())
