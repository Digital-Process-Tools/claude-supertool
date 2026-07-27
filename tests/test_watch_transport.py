"""Unit tests for presets/watch/transport.py event emission + state I/O."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

WATCH_DIR = Path(__file__).parent.parent / "presets" / "watch"
sys.path.insert(0, str(WATCH_DIR))

_spec = importlib.util.spec_from_file_location("watch_transport", WATCH_DIR / "transport.py")
assert _spec is not None and _spec.loader is not None
transport = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(transport)


def test_state_path_naming() -> None:
    assert transport.state_path("gitlab-mr", "21803").endswith("supertool-watch-gitlab-mr__21803.state.json")


def test_pid_path_naming() -> None:
    assert transport.pid_path("gitlab-mr", "21803").endswith("supertool-watch-gitlab-mr__21803.pid")


def test_write_and_read_state_roundtrip(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path))
    payload = {"mr_state": "opened", "pipeline_status": "running"}
    transport.write_state("gitlab-mr", "21803", payload)
    out = transport.read_state("gitlab-mr", "21803")
    assert out == payload


def test_read_state_missing_returns_empty(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path))
    assert transport.read_state("gitlab-mr", "does-not-exist") == {}


def test_read_state_corrupt_returns_empty(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path))
    p = Path(transport.state_path("gitlab-mr", "21803"))
    p.write_text("{not json")
    assert transport.read_state("gitlab-mr", "21803") == {}


def test_emit_socket_no_listener_silent(monkeypatch, tmp_path) -> None:
    """When the socket file doesn't exist, emit_socket is a no-op (no exception)."""
    monkeypatch.setattr(transport, "SOCK_PATH", str(tmp_path / "nonexistent.sock"))
    transport.emit_socket({"event": "test"})


def test_emit_event_writes_state_with_last_event(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(transport, "SOCK_PATH", str(tmp_path / "nonexistent.sock"))
    monkeypatch.setattr(transport, "desktop_notify", lambda *a, **kw: None)
    transport.emit_event(
        "gitlab-mr", "21803",
        "pipeline_failed",
        {"pipeline_id": "139928"},
    )
    state = transport.read_state("gitlab-mr", "21803")
    assert state["last_event"]["event"] == "pipeline_failed"
    assert state["last_event"]["source"] == "gitlab-mr"
    assert state["last_event"]["id"] == "21803"
    assert state["last_event"]["payload"]["pipeline_id"] == "139928"
    assert "first_seen" in state


def test_emit_event_preserves_existing_state_keys(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(transport, "SOCK_PATH", str(tmp_path / "nonexistent.sock"))
    transport.write_state("gitlab-mr", "21803", {"source_state": {"keep": "me"}})
    transport.emit_event("gitlab-mr", "21803", "merged", {})
    state = transport.read_state("gitlab-mr", "21803")
    assert state["source_state"] == {"keep": "me"}
    assert state["last_event"]["event"] == "merged"


def test_desktop_notify_noop_off_macos(monkeypatch) -> None:
    monkeypatch.setattr(transport.sys, "platform", "linux")
    # Should not raise even though osascript wouldn't work
    transport.desktop_notify("title", "message")


# ---------------------------------------------------------------------------
# _pid_alive — radar's idempotence is built on this answering, not raising
# ---------------------------------------------------------------------------

class _FakeKernel32:
    """Stubs the three read-only calls the probe is allowed to make.

    Any other attribute raises, so a probe that reached for TerminateProcess
    (what `os.kill(pid, 0)` does on Windows) fails loudly instead of silently
    killing the watcher it was asked about.
    """

    def __init__(self, handle=1234, exit_code=259, query_ok=True):
        self.handle = handle
        self.exit_code = exit_code
        self.query_ok = query_ok
        self.opened: list = []
        self.closed: list = []

    def OpenProcess(self, access, inherit, pid):
        self.opened.append((access, inherit, pid))
        return self.handle

    def GetExitCodeProcess(self, handle, ptr):
        if not self.query_ok:
            return 0
        ptr._obj.value = self.exit_code
        return 1

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1

    def __getattr__(self, name):
        raise AssertionError(f"the liveness probe must not call {name}")


def _fake_windows(monkeypatch, **kw) -> _FakeKernel32:
    fake = _FakeKernel32(**kw)
    monkeypatch.setattr(transport, "_kernel32", lambda: fake)
    return fake


def test_pid_alive_rejects_a_nonpositive_pid() -> None:
    assert transport._pid_alive(0) is False
    assert transport._pid_alive(-1) is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX os.kill semantics")
def test_pid_alive_says_yes_for_our_own_process() -> None:
    assert transport._pid_alive(os.getpid()) is True


def test_pid_alive_says_no_for_a_pid_that_cannot_exist() -> None:
    """The regression: on Windows this raised WinError 87 out of radar."""
    assert transport._pid_alive(9999999) is False


def test_windows_probe_reports_a_running_process_as_alive(monkeypatch) -> None:
    _fake_windows(monkeypatch, exit_code=259)
    assert transport._pid_alive_windows(4242) is True


def test_windows_probe_reports_an_exited_process_as_dead(monkeypatch) -> None:
    _fake_windows(monkeypatch, exit_code=0)
    assert transport._pid_alive_windows(4242) is False


def test_windows_probe_reports_a_missing_process_as_dead(monkeypatch) -> None:
    _fake_windows(monkeypatch, handle=0)
    assert transport._pid_alive_windows(4242) is False


def test_windows_probe_treats_an_unreadable_exit_code_as_dead(monkeypatch) -> None:
    _fake_windows(monkeypatch, query_ok=False)
    assert transport._pid_alive_windows(4242) is False


def test_windows_probe_asks_only_for_query_rights(monkeypatch) -> None:
    """PROCESS_ALL_ACCESS (0x1F0FFF) would hand the probe the power to kill."""
    fake = _fake_windows(monkeypatch)
    transport._pid_alive_windows(4242)
    access, _inherit, pid = fake.opened[0]
    assert access == 0x1000
    assert pid == 4242


def test_windows_probe_closes_the_handle(monkeypatch) -> None:
    fake = _fake_windows(monkeypatch, handle=77)
    transport._pid_alive_windows(4242)
    assert fake.closed == [77]


def test_windows_probe_closes_the_handle_even_when_the_query_fails(monkeypatch) -> None:
    fake = _fake_windows(monkeypatch, handle=77, query_ok=False)
    transport._pid_alive_windows(4242)
    assert fake.closed == [77]


def test_pid_alive_uses_the_windows_probe_on_win32(monkeypatch) -> None:
    monkeypatch.setattr(transport.sys, "platform", "win32")
    seen: list[int] = []
    monkeypatch.setattr(transport, "_pid_alive_windows",
                        lambda pid: (seen.append(pid), True)[1])
    assert transport._pid_alive(4242) is True
    assert seen == [4242]


def test_pid_alive_never_uses_os_kill_on_win32(monkeypatch) -> None:
    """os.kill(pid, 0) on Windows routes to TerminateProcess — it would kill
    the watcher rather than report on it."""
    monkeypatch.setattr(transport.sys, "platform", "win32")
    _fake_windows(monkeypatch)

    def _forbidden(*_a, **_k):
        raise AssertionError("os.kill must not be used for liveness on Windows")

    monkeypatch.setattr(transport.os, "kill", _forbidden)
    assert transport._pid_alive(4242) is True


def test_a_raising_windows_probe_resolves_to_not_alive(monkeypatch) -> None:
    """An unanswerable question must not propagate — radar would crash."""
    monkeypatch.setattr(transport.sys, "platform", "win32")

    def _boom(_pid):
        raise OSError(87, "The parameter is incorrect")

    monkeypatch.setattr(transport, "_pid_alive_windows", _boom)
    assert transport._pid_alive(4242) is False


def test_an_unexpected_oserror_on_posix_resolves_to_not_alive(monkeypatch) -> None:
    monkeypatch.setattr(transport.sys, "platform", "linux")

    def _boom(_pid, _sig):
        raise OSError(87, "The parameter is incorrect")

    monkeypatch.setattr(transport.os, "kill", _boom)
    assert transport._pid_alive(4242) is False
