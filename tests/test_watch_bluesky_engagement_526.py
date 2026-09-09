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
from pathlib import Path

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
