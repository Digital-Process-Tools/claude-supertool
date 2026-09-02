"""Every Slack poller on a host shared one 500-file eviction pool, keyed by
nothing at all -- so a busy channel's burst evicted a quiet channel's older
message-body files before any agent had opened them (issue #2149).

#2044 moved a free-form message body out of the channel event and into a
file, leaving the event holding `payload_path`, `length` and `sha256`. If the
file behind that path is gone, the body is gone -- and before this fix, a
message on channel A could vanish because channel B received a burst.

The fix scopes the eviction pool per channel (#2149's first option): each
watched channel gets its own subdirectory under `slack-messages/`, so a
burst on one channel can only ever evict that same channel's own files.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

WATCH_DIR = Path(__file__).parent.parent / "presets" / "watch"


def _load_poller():
    sys.path.insert(0, str(WATCH_DIR))
    spec = importlib.util.spec_from_file_location(
        "watch_slack_poller_2149", WATCH_DIR / "sources" / "slack" / "poller.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_two_channels_get_two_different_pools(tmp_path, monkeypatch) -> None:
    poller = _load_poller()
    monkeypatch.setattr(poller.transport, "STATE_DIR", str(tmp_path))
    a = poller._message_store_dir("C_QUIET")
    b = poller._message_store_dir("C_BUSY")
    assert a != b
    assert a.is_dir() and b.is_dir()


def test_a_burst_on_one_channel_cannot_evict_another_channels_files(
        tmp_path, monkeypatch) -> None:
    """The defect itself: before this fix, both files below landed in one
    shared pool and the busy channel's burst evicted the quiet channel's
    only message. Positive control on the eviction mechanism (`test_channel
    == quiet` half must fire): `_MESSAGE_RETENTION_MAX` is lowered so a
    two-file burst on the busy channel alone is enough to prove the cap only
    ever bites its own channel's pool."""
    poller = _load_poller()
    monkeypatch.setattr(poller.transport, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(poller, "_MESSAGE_RETENTION_MAX", 1)

    quiet_path = poller._write_message_file(
        "C_QUIET", None, "1.0", "false", "text", "the only message on a quiet channel")

    # A burst on a different, busy channel: two writes against a
    # per-channel cap of 1, which evicts the busy channel's own first file
    # and must never touch the quiet channel's.
    busy_first = poller._write_message_file(
        "C_BUSY", None, "1.0", "false", "text", "busy message one")
    busy_second = poller._write_message_file(
        "C_BUSY", None, "2.0", "false", "text", "busy message two")

    assert Path(quiet_path).exists(), (
        "the quiet channel's file was evicted by a burst on another channel "
        "-- this is #2149 itself")
    assert not Path(busy_first).exists(), (
        "must fire: the busy channel's own cap must still evict its own "
        "oldest file, or this test cannot tell isolation from no eviction "
        "at all")
    assert Path(busy_second).exists()


def test_channel_isolation_survives_a_hash_collision_free_range(
        tmp_path, monkeypatch) -> None:
    """Same channel string used twice must resolve to the same pool, so the
    per-channel scoping does not itself fragment one channel's history."""
    poller = _load_poller()
    monkeypatch.setattr(poller.transport, "STATE_DIR", str(tmp_path))
    assert poller._message_store_dir("C1") == poller._message_store_dir("C1")
