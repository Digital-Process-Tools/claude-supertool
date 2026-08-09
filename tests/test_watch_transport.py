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


def test_emit_socket_no_listener_does_not_raise(monkeypatch, tmp_path) -> None:
    """No socket file: emit_socket still never raises. Since #554 it is no longer
    *silent* either — it returns the definite negative, pinned in
    tests/test_watch_channel_health_554.py."""
    monkeypatch.setattr(transport, "SOCK_PATH", str(tmp_path / "nonexistent.sock"))
    assert transport.emit_socket({"event": "test"}).state == transport.EMIT_NO_LISTENER


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


# ---------------------------------------------------------------------------
# #464 — a first-tick emission is a report of the state a watcher *found*, not
# of a change it *observed*. Both are worth emitting; they must not look alike.
# ---------------------------------------------------------------------------

def _capture_into(seen: list) -> object:
    """Stand in for `emit_socket` and return the verdict `emit_event` reads.

    Since #554 `emit_socket` reports which of three things its write meant, and
    `emit_event` records that in the state file. A stub returning None makes
    `emit_event` raise, which is the right shape: the verdict is part of the
    contract now, not an optional extra.
    """
    def _capture(record: dict) -> transport.Emit:
        seen.append(record)
        return transport.Emit(transport.EMIT_ACCEPTED, "captured")

    return _capture


def test_the_emitted_record_carries_first_tick(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(transport, "SOCK_PATH", str(tmp_path / "nonexistent.sock"))
    seen: list[dict] = []
    monkeypatch.setattr(transport, "emit_socket", _capture_into(seen))
    transport.emit_event("gitlab-mr", "32912", "pipeline_succeeded", {}, first_tick=True)
    assert seen[0]["first_tick"] is True


def test_a_live_transition_is_not_marked_first_tick(monkeypatch, tmp_path) -> None:
    """The false-positive direction — a marker that is always on says nothing."""
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(transport, "SOCK_PATH", str(tmp_path / "nonexistent.sock"))
    seen: list[dict] = []
    monkeypatch.setattr(transport, "emit_socket", _capture_into(seen))
    transport.emit_event("gitlab-mr", "32912", "pipeline_succeeded", {})
    assert seen[0]["first_tick"] is False


def test_first_tick_is_always_present_on_the_record(monkeypatch, tmp_path) -> None:
    """Present-and-false, never absent: a consumer reading the key must not
    have to decide what a missing key meant."""
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(transport, "SOCK_PATH", str(tmp_path / "nonexistent.sock"))
    transport.emit_event("gitlab-mr", "32912", "merged", {})
    assert transport.read_state("gitlab-mr", "32912")["last_event"]["first_tick"] is False


def test_the_locked_payload_did_not_move(monkeypatch, tmp_path) -> None:
    """`first_tick` is a property of the emission, like `ts` — it belongs
    beside the envelope keys and not inside the source-defined payload, which
    docs/presets/watch.md locks."""
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(transport, "SOCK_PATH", str(tmp_path / "nonexistent.sock"))
    seen: list[dict] = []
    monkeypatch.setattr(transport, "emit_socket", _capture_into(seen))
    transport.emit_event("gitlab-mr", "32912", "merged", {"url": "u", "title": "t"},
                         first_tick=True)
    assert seen[0]["payload"] == {"url": "u", "title": "t"}


def test_the_channel_consumer_surfaces_first_tick() -> None:
    """The bridge to Claude is where #464 was actually paid for — a marker the
    consumer drops is a marker that does not exist."""
    channel = Path(__file__).parent.parent / "notifiers" / "claude-channel" / "channel.ts"
    body = channel.read_text(encoding="utf-8")
    assert "first_tick" in body


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
