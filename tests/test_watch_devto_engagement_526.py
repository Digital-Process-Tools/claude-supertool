"""Tests for the devto-engagement watch source (#526).

`devto_status_since` already does this fetch by hand, on demand: list your
published articles, then list comments on each of them. It exists because
dev.to gives no push channel, and something can land on any article at any
time -- which is a watcher's job. This source is that fetch taught to run
itself, as a population poller (a scope, not one article id) rather than a
per-id watcher, mirroring `gitlab-mr-feed`/`github-issue-feed`.

The tests pin: a previously-unseen source baselines silently on its first
tick (no flood of "new" comments/reactions for content that already
existed); a second tick with a genuine new comment fires the right event,
and a reply to your own comment (per the local outbound ledger) is told
apart from an ordinary new comment; a reaction count crossing a declared
threshold fires once, never as a bare running count; and a fetch failure
reports as `engagement_unreachable` rather than emptying the population.
"""
from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).parent.parent
SOURCE_DIR = REPO / "presets" / "watch" / "sources" / "devto-engagement"

_spec = importlib.util.spec_from_file_location("devto_engagement_poller", SOURCE_DIR / "poller.py")
assert _spec is not None and _spec.loader is not None
feed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(feed)

CTX = {"source": "devto-engagement", "id": "@me", "only": []}


def _article(aid="100", reactions=0):
    return {aid: {"title": f"article {aid}", "url": f"https://dev.to/x/{aid}", "reactions": reactions}}


def _rig(monkeypatch, api_key="key", my_ids=None):
    monkeypatch.setattr(feed, "_resolve_api_key", lambda: api_key)
    monkeypatch.setattr(feed, "my_comment_ids", lambda records: my_ids or set())
    monkeypatch.setattr(feed, "read_outbound", lambda: [])


def _population(monkeypatch, pop):
    monkeypatch.setattr(feed, "fetch_population", lambda scope, api_key: (pop, ""))


def _comments(monkeypatch, mapping):
    """mapping: {aid: [{"id":..., "parent":...}, ...]}"""
    def _fetch(aid, api_key):
        return mapping.get(aid, []), ""
    monkeypatch.setattr(feed, "fetch_comment_ids", _fetch)


def _keys(events):
    return [e["event"] for e in events]


def test_first_poll_over_an_unseen_article_baselines_silently(monkeypatch):
    _rig(monkeypatch)
    _population(monkeypatch, _article("100", reactions=40))
    _comments(monkeypatch, {"100": [{"id": "c1", "parent": ""}, {"id": "c2", "parent": ""}]})
    events, state = feed.poll({}, CTX)
    assert events == []
    assert sorted(state["known"]["100"]["comment_ids"]) == ["c1", "c2"]
    assert state["known"]["100"]["reactions"] == 40


def test_a_new_comment_on_the_second_tick_fires_comment_received(monkeypatch):
    _rig(monkeypatch)
    _population(monkeypatch, _article("100"))
    _comments(monkeypatch, {"100": [{"id": "c1", "parent": ""}]})
    _, state = feed.poll({}, CTX)
    _comments(monkeypatch, {"100": [{"id": "c1", "parent": ""}, {"id": "c2", "parent": ""}]})
    events, _ = feed.poll(state, CTX)
    assert _keys(events) == ["comment_received"]
    assert events[0]["payload"]["comment"] == "c2"


def test_a_reply_to_my_own_comment_fires_reply_received_instead(monkeypatch):
    """`c1` was posted through this tool (in the outbound ledger), so a new
    comment whose parent is `c1` is a reply to me, not ordinary new
    engagement — and must not also fire `comment_received`."""
    _rig(monkeypatch, my_ids={"c1"})
    _population(monkeypatch, _article("100"))
    _comments(monkeypatch, {"100": [{"id": "c1", "parent": ""}]})
    _, state = feed.poll({}, CTX)
    _comments(monkeypatch, {"100": [{"id": "c1", "parent": ""}, {"id": "c2", "parent": "c1"}]})
    events, _ = feed.poll(state, CTX)
    assert _keys(events) == ["reply_received"]
    assert events[0]["payload"]["parent"] == "c1"


def test_a_baseline_comment_fetch_failure_does_not_lock_in_a_false_empty_set(monkeypatch):
    """A first-seen article whose /comments call fails on that very tick must
    not record an empty comment_ids baseline -- otherwise the next
    successful fetch diffs the article's whole pre-existing comment set
    against that false empty set and announces every one of them as new
    (found in review; #526)."""
    _rig(monkeypatch)
    _population(monkeypatch, _article("100"))

    def _fail(aid, api_key):
        return None, "ERROR: dev.to timed out"

    monkeypatch.setattr(feed, "fetch_comment_ids", _fail)
    events, state = feed.poll({}, CTX)
    assert events == []
    assert "100" not in state["known"]

    _comments(monkeypatch, {"100": [{"id": "c1", "parent": ""}, {"id": "c2", "parent": ""}]})
    events, state = feed.poll(state, CTX)
    assert events == []
    assert sorted(state["known"]["100"]["comment_ids"]) == ["c1", "c2"]


def test_the_max_articles_knob_matches_status_since(monkeypatch):
    """The same env var `devto_status_since` reads (`SUPERTOOL_STATUS_POSTS`)
    decides how many articles this source covers too, so the two agree
    about what "recent" means for one account instead of guessing
    separately (found in review; #526's docs claimed this before the code
    did it)."""
    monkeypatch.setenv("SUPERTOOL_STATUS_POSTS", "3")
    seen = {}

    def _fake_get(path, api_key, query=None, timeout=20):
        seen["query"] = query
        return [], ""

    monkeypatch.setattr(feed, "_get", _fake_get)
    feed.fetch_population("@me", "key")
    assert seen["query"]["per_page"] == 3


def test_a_reaction_crossing_a_threshold_fires_once(monkeypatch):
    _rig(monkeypatch)
    _population(monkeypatch, _article("100", reactions=9))
    _comments(monkeypatch, {"100": []})
    _, state = feed.poll({}, CTX)
    _population(monkeypatch, _article("100", reactions=15))
    events, _ = feed.poll(state, CTX)
    assert _keys(events) == ["reaction_received"]
    assert events[0]["payload"]["threshold"] == 10


def test_reactions_already_above_threshold_on_baseline_are_not_reported(monkeypatch):
    """A threshold crossed before this poller ever looked is not news --
    the same rule that keeps a feed's first poll silent about a population
    it just opened on."""
    _rig(monkeypatch)
    _population(monkeypatch, _article("100", reactions=999))
    _comments(monkeypatch, {"100": []})
    events, _ = feed.poll({}, CTX)
    assert events == []


def test_no_bare_reaction_count_event_exists():
    """#526 is explicit: a reaction total with no nameable before/after must
    either become a threshold crossing or be left out entirely -- never a
    bare running-count event."""
    assert "reaction_count" not in feed.EVENT_KEYS
    assert "reaction_received" in feed.EVENT_KEYS


def test_a_fetch_failure_reports_unreachable_not_an_empty_population(monkeypatch):
    monkeypatch.setattr(feed, "_resolve_api_key", lambda: "key")
    monkeypatch.setattr(feed, "fetch_population",
                        lambda scope, api_key: (None, "ERROR: dev.to timed out"))
    events, state = feed.poll({}, CTX)
    assert _keys(events) == ["engagement_unreachable"]
    assert state["lookup"] == "unavailable"


def test_unreachable_fires_once_per_outage_not_once_per_poll(monkeypatch):
    monkeypatch.setattr(feed, "_resolve_api_key", lambda: "key")
    monkeypatch.setattr(feed, "fetch_population",
                        lambda scope, api_key: (None, "ERROR: down"))
    _, state = feed.poll({}, CTX)
    events, _ = feed.poll(state, CTX)
    assert events == []


def test_missing_api_key_is_unreachable_not_a_crash(monkeypatch):
    monkeypatch.setattr(feed, "_resolve_api_key", lambda: None)
    events, state = feed.poll({}, CTX)
    assert _keys(events) == ["engagement_unreachable"]


def test_the_source_never_terminates():
    for state in ({}, {"known": {}}, {"lookup": "unavailable"}):
        assert feed.is_terminal(state) is False


def test_the_interval_is_generous():
    """Rate limits are the real constraint, not correctness (#526)."""
    assert feed.INTERVAL >= 300


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
