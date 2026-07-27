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
# _pid_alive — one shared probe, tested in tests/test_proc.py
# ---------------------------------------------------------------------------

def test_transport_uses_the_shared_probe() -> None:
    """radar's idempotence is built on this answering, not raising — and the
    answer must come from presets/_proc.py, not a local copy that can drift."""
    presets = Path(__file__).parent.parent / "presets"
    sys.path.insert(0, str(presets))
    import _proc

    assert transport._pid_alive is _proc.pid_alive
