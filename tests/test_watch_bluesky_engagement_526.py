"""Tests for the bluesky-engagement watch source (#526).

`bluesky_status_since` already fetches exactly this -- the native
notifications endpoint, filtered to what is new since a timestamp -- because
Bluesky gives no push channel either. This source is that fetch taught to
run itself, as a population poller over the notification feed.

Unlike dev.to's aggregate reaction count, a Bluesky like/repost arrives as
its own notification with its own author, subject and timestamp -- a real,
nameable event -- so this source reports each one directly rather than
reducing it to a running total first. The tests pin: silent baselining on
the first tick, a genuine new reply/mention/like firing the right event on
a later tick, and a fetch failure reporting as unreachable.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
SOURCE_DIR = REPO / "presets" / "watch" / "sources" / "bluesky-engagement"

_spec = importlib.util.spec_from_file_location("bluesky_engagement_poller", SOURCE_DIR / "poller.py")
assert _spec is not None and _spec.loader is not None
feed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(feed)

CTX = {"source": "bluesky-engagement", "id": "@me", "only": []}


def _notif(uri, reason, handle="alice.bsky.social", text="hi"):
    return {"uri": uri, "reason": reason, "author": {"handle": handle},
            "record": {"text": text}, "reasonSubject": "at://post/1"}


def _feed_(monkeypatch, notifications):
    monkeypatch.setattr(feed, "fetch_notifications", lambda scope: (notifications, ""))


def _keys(events):
    return [e["event"] for e in events]


def test_first_poll_baselines_silently(monkeypatch):
    """A reply that already existed when the poller started is not news."""
    _feed_(monkeypatch, [_notif("at://n/1", "reply"), _notif("at://n/2", "like")])
    events, state = feed.poll({}, CTX)
    assert events == []
    assert set(state["seen"]) == {"at://n/1", "at://n/2"}


def test_a_new_reply_on_the_second_tick_fires_reply_received(monkeypatch):
    _feed_(monkeypatch, [_notif("at://n/1", "reply")])
    _, state = feed.poll({}, CTX)
    _feed_(monkeypatch, [_notif("at://n/1", "reply"), _notif("at://n/2", "reply")])
    events, _ = feed.poll(state, CTX)
    assert _keys(events) == ["reply_received"]
    assert events[0]["payload"]["uri"] == "at://n/2"


def test_a_new_like_fires_reaction_received_directly_no_threshold_needed(monkeypatch):
    """Each Bluesky like is its own identified notification -- a nameable
    before/after -- unlike dev.to's aggregate count, so no crossing logic
    is needed for this source to report it honestly."""
    _feed_(monkeypatch, [])
    _, state = feed.poll({}, CTX)
    _feed_(monkeypatch, [_notif("at://n/9", "like")])
    events, _ = feed.poll(state, CTX)
    assert _keys(events) == ["reaction_received"]


def test_a_new_mention_fires_comment_received(monkeypatch):
    _feed_(monkeypatch, [])
    _, state = feed.poll({}, CTX)
    _feed_(monkeypatch, [_notif("at://n/9", "mention")])
    events, _ = feed.poll(state, CTX)
    assert _keys(events) == ["comment_received"]


def test_a_follow_notification_emits_nothing(monkeypatch):
    """Not one of the three events #526 asks for -- a follow is not
    engagement with a post, and forcing it into reaction_received would
    misdescribe it."""
    _feed_(monkeypatch, [])
    _, state = feed.poll({}, CTX)
    _feed_(monkeypatch, [_notif("at://n/9", "follow")])
    events, _ = feed.poll(state, CTX)
    assert events == []


def test_a_fetch_failure_reports_unreachable(monkeypatch):
    monkeypatch.setattr(feed, "fetch_notifications", lambda scope: (None, "ERROR: down"))
    events, state = feed.poll({}, CTX)
    assert _keys(events) == ["engagement_unreachable"]
    assert state["lookup"] == "unavailable"


def test_unreachable_fires_once_per_outage(monkeypatch):
    monkeypatch.setattr(feed, "fetch_notifications", lambda scope: (None, "ERROR: down"))
    _, state = feed.poll({}, CTX)
    events, _ = feed.poll(state, CTX)
    assert events == []


def test_the_source_never_terminates():
    for state in ({}, {"seen": []}, {"lookup": "unavailable"}):
        assert feed.is_terminal(state) is False


def test_the_interval_is_generous():
    assert feed.INTERVAL >= 60


def _emitted_event_keys():
    tree = ast.parse((SOURCE_DIR / "poller.py").read_text(encoding="utf-8"))
    keys = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (isinstance(key, ast.Constant) and key.value == "event"
                        and isinstance(value, ast.Constant)
                        and isinstance(value.value, str)):
                    keys.add(value.value)
    return keys


def test_events_json_lists_exactly_what_the_poller_emits():
    declared = {e["key"] for e in json.loads(
        (SOURCE_DIR / "events.json").read_text(encoding="utf-8"))["events"]}
    assert declared == _emitted_event_keys()


def test_the_modules_own_declaration_matches_what_it_emits():
    assert set(feed.EVENT_KEYS) == _emitted_event_keys()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX file modes; Windows os.chmod only toggles read-only",
)
def test_save_session_never_world_or_group_readable(tmp_path, monkeypatch):
    """#2484: same write-then-chmod window as `presets/bluesky/_atproto.py`
    -- `_save_session` here is its own separate copy (this module
    deliberately never imports `_atproto.py`, see the module docstring),
    so the fix has to land here independently too. Mirrors
    `tests/test_security_bluesky.py::TestSessionFileNeverWideOpen`: drive
    the write under a permissive umask and catch the file existing at a
    wider mode than 0o600 at any point, not just after the call returns.
    """
    session_file = tmp_path / "session.json"
    monkeypatch.setattr(feed, "SESSION_FILE", session_file)

    old_umask = os.umask(0)  # nothing masked away -- worst case
    real_open = os.open
    real_fchmod = getattr(os, "fchmod", None)
    real_chmod = os.chmod
    open_modes = []
    pre_narrow_modes = []

    def spying_open(path, flags, mode=0o777, *a, **kw):
        fd = real_open(path, flags, mode, *a, **kw)
        if os.fspath(path) == os.fspath(session_file) and (flags & os.O_CREAT):
            open_modes.append(mode & 0o777)
        return fd

    def spying_fchmod(fd, mode, *a, **kw):
        pre_narrow_modes.append(stat.S_IMODE(os.fstat(fd).st_mode))
        return real_fchmod(fd, mode, *a, **kw)

    def spying_chmod(path, mode, *a, **kw):
        if os.fspath(path) == os.fspath(session_file) and os.path.exists(path):
            pre_narrow_modes.append(stat.S_IMODE(os.stat(path).st_mode))
        return real_chmod(path, mode, *a, **kw)

    os.open = spying_open
    if real_fchmod is not None:
        os.fchmod = spying_fchmod
    os.chmod = spying_chmod
    try:
        feed._save_session({"accessJwt": "x", "refreshJwt": "y"})
    finally:
        os.umask(old_umask)
        os.open = real_open
        if real_fchmod is not None:
            os.fchmod = real_fchmod
        os.chmod = real_chmod

    assert open_modes, (
        "os.open was never used to create the session file atomically -- "
        "nothing here proves the write-then-chmod window is closed"
    )
    assert real_fchmod is not None, (
        "os.fchmod is unavailable on this platform -- this test only "
        "runs where it should be exercised (skipif win32 above)"
    )
    assert pre_narrow_modes, (
        "os.fchmod was never called to narrow the file -- the "
        "TOCTOU-safe fd-based narrowing path went unexercised"
    )
    assert all(m == 0o600 for m in open_modes), [oct(m) for m in open_modes]
    assert all(m == 0o600 for m in pre_narrow_modes), [oct(m) for m in pre_narrow_modes]
    assert stat.S_IMODE(session_file.stat().st_mode) == 0o600


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX file modes; Windows os.chmod only toggles read-only",
)
def test_save_session_narrows_a_pre_existing_wide_file(tmp_path, monkeypatch):
    """The fresh-file case above is trivially narrow because os.open()'s
    own mode argument only applies to a file *it* creates -- POSIX
    ignores that argument for a file that already exists. The actual
    job of the fchmod/chmod call this test targets only shows up against
    a file that pre-exists wide (e.g. left over from before #2484's fix,
    or from a umask that widened an earlier write): this pins that
    _save_session narrows it rather than leaving it as-is.
    """
    session_file = tmp_path / "session.json"
    session_file.write_text('{"stale": true}', encoding="utf-8")
    os.chmod(session_file, 0o644)
    assert stat.S_IMODE(session_file.stat().st_mode) == 0o644  # precondition

    monkeypatch.setattr(feed, "SESSION_FILE", session_file)
    feed._save_session({"accessJwt": "x", "refreshJwt": "y"})

    assert stat.S_IMODE(session_file.stat().st_mode) == 0o600
