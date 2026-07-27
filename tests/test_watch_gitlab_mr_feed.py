"""Tests for the gitlab-mr-feed watch source (issue #422).

Every other source polls one known id, so nothing in the running system could
discover an MR that did not exist at spawn time. This source polls the
population. The tests therefore pin the two claims that gap turns on — a new
iid gets *that exact iid* watched and announced, and a vanished iid is
described by what actually happened to it rather than by a guess — plus the
failure modes that would restore the silence: a fetch failure must not read as
"everything vanished", and the feed must never stop itself.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
SOURCE_DIR = REPO / "presets" / "watch" / "sources" / "gitlab-mr-feed"

_spec = importlib.util.spec_from_file_location("mr_feed_poller", SOURCE_DIR / "poller.py")
assert _spec is not None and _spec.loader is not None
feed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(feed)

CTX = {"source": "gitlab-mr-feed", "id": "@me", "only": []}


def _pop(*iids: int) -> dict[str, dict[str, str]]:
    return {str(i): {"title": f"title {i}", "web_url": f"https://gl/mr/{i}"} for i in iids}


def _keys(events: list[dict]) -> list[str]:
    return [e["event"] for e in events]


def _by_key(events: list[dict], key: str) -> dict:
    return next(e for e in events if e["event"] == key)


@pytest.fixture
def rig(monkeypatch):
    """Stub every outward call; record spawns, stops and lookups."""
    calls: dict = {"spawned": [], "stopped": [], "looked_up": [], "watched": set()}
    monkeypatch.setattr(feed, "live_watchers", lambda: set(calls["watched"]))
    monkeypatch.setattr(feed, "spawn_watcher",
                        lambda iid: (calls["spawned"].append(iid), True)[1])
    monkeypatch.setattr(feed, "stop_watcher", lambda iid: calls["stopped"].append(iid))
    return {"calls": calls, "monkeypatch": monkeypatch}


def _population(rig, *pops):
    """Queue one population per poll; the last one repeats."""
    seq = list(pops)

    def _fetch(_scope):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    rig["monkeypatch"].setattr(feed, "fetch_population", _fetch)


def _lookup(rig, mapping):
    def _do(iid):
        rig["calls"]["looked_up"].append(iid)
        return mapping.get(iid, "")

    rig["monkeypatch"].setattr(feed, "lookup_mr_state", _do)


# ---------------------------------------------------------------------------
# scope resolution — the caller picks the population
# ---------------------------------------------------------------------------

def test_me_alias_resolves_to_my_open_mrs() -> None:
    assert feed.resolve_filter("@me") == "author=@me,state=opened"


def test_reviewer_alias_covers_mrs_i_owe_a_review() -> None:
    """A team radar blind to review requests is blind to half the job."""
    assert feed.resolve_filter("@reviewer") == "reviewer=@me,state=opened"


def test_an_unknown_scope_is_used_as_a_literal_gl_mrs_filter() -> None:
    assert feed.resolve_filter("milestone=v18.9") == "milestone=v18.9"


# ---------------------------------------------------------------------------
# first poll — baseline, not a notification storm
# ---------------------------------------------------------------------------

def test_first_poll_announces_nothing(rig) -> None:
    """Every MR open when the feed starts is not a discovery."""
    _population(rig, _pop(33175, 19564))
    events, state = feed.poll({}, CTX)
    assert events == []
    assert sorted(state["known"]) == ["19564", "33175"]


def test_first_poll_still_covers_unwatched_mrs(rig) -> None:
    _population(rig, _pop(33175, 19564))
    rig["calls"]["watched"] = {"19564"}
    feed.poll({}, CTX)
    assert rig["calls"]["spawned"] == ["33175"]


# ---------------------------------------------------------------------------
# discovery — the gap this source exists to close
# ---------------------------------------------------------------------------

def test_a_new_iid_is_watched_and_announced(rig) -> None:
    """Florian's !33176: opened between two radar runs, invisible until the
    second one. It must now surface with no one typing anything."""
    _population(rig, _pop(33175), _pop(33175, 33176))
    _, state = feed.poll({}, CTX)
    rig["calls"]["watched"] = {"33175"}
    rig["calls"]["spawned"].clear()
    events, state = feed.poll(state, CTX)
    assert rig["calls"]["spawned"] == ["33176"]
    assert _keys(events) == ["mr_opened"]
    assert _by_key(events, "mr_opened")["payload"]["iid"] == "33176"


def test_mr_opened_carries_the_title_and_url(rig) -> None:
    _population(rig, _pop(33175), _pop(33175, 33176))
    _, state = feed.poll({}, CTX)
    events, _ = feed.poll(state, CTX)
    payload = _by_key(events, "mr_opened")["payload"]
    assert payload["title"] == "title 33176"
    assert payload["url"] == "https://gl/mr/33176"


def test_mr_opened_notifies_because_nothing_else_can_report_it(rig) -> None:
    _population(rig, _pop(33175), _pop(33175, 33176))
    _, state = feed.poll({}, CTX)
    events, _ = feed.poll(state, CTX)
    ev = _by_key(events, "mr_opened")
    assert ev["notify_title"] == "!33176 opened"
    assert ev["notify_message"] == "title 33176"


def test_an_unchanged_population_announces_nothing(rig) -> None:
    _population(rig, _pop(33175, 19564))
    _, state = feed.poll({}, CTX)
    rig["calls"]["watched"] = {"33175", "19564"}
    events, _ = feed.poll(state, CTX)
    assert events == []
    assert rig["calls"]["spawned"] == ["33175", "19564"]


def test_a_known_mr_whose_watcher_died_is_recovered(rig) -> None:
    """Coverage is continuous for the same reason discovery is."""
    _population(rig, _pop(33175, 19564))
    _, state = feed.poll({}, CTX)
    rig["calls"]["watched"] = {"33175"}
    rig["calls"]["spawned"].clear()
    feed.poll(state, CTX)
    assert rig["calls"]["spawned"] == ["19564"]


# ---------------------------------------------------------------------------
# departure — vanished is not the same claim as merged
# ---------------------------------------------------------------------------

def test_a_vanished_iid_that_merged_reports_merged_and_stops_its_watcher(rig) -> None:
    _population(rig, _pop(33175, 33176), _pop(33175))
    _lookup(rig, {"33176": "merged"})
    _, state = feed.poll({}, CTX)
    events, _ = feed.poll(state, CTX)
    assert _keys(events) == ["mr_merged"]
    assert rig["calls"]["stopped"] == ["33176"]


def test_a_vanished_iid_that_closed_is_not_reported_as_merged(rig) -> None:
    """Inventing mr_merged for a closed MR is the confident-wrong class."""
    _population(rig, _pop(33175, 33176), _pop(33175))
    _lookup(rig, {"33176": "closed"})
    _, state = feed.poll({}, CTX)
    events, _ = feed.poll(state, CTX)
    assert _keys(events) == ["mr_closed"]
    assert rig["calls"]["looked_up"] == ["33176"]


def test_a_vanished_iid_still_open_is_reported_honestly_and_kept_watched(rig) -> None:
    """Reassigned, or the filter changed. Neither is an ending, and following
    an MR the feed no longer returns is legitimate."""
    _population(rig, _pop(33175, 33176), _pop(33175))
    _lookup(rig, {"33176": "opened"})
    _, state = feed.poll({}, CTX)
    events, _ = feed.poll(state, CTX)
    assert _keys(events) == ["mr_left_feed"]
    assert _by_key(events, "mr_left_feed")["payload"]["mr_state"] == "opened"
    assert rig["calls"]["stopped"] == []


def test_a_vanished_iid_that_cannot_be_looked_up_says_unknown(rig) -> None:
    _population(rig, _pop(33175, 33176), _pop(33175))
    _lookup(rig, {})
    _, state = feed.poll({}, CTX)
    events, _ = feed.poll(state, CTX)
    assert _by_key(events, "mr_left_feed")["payload"]["mr_state"] == "unknown"
    assert rig["calls"]["stopped"] == []


def test_merged_and_closed_do_not_fire_a_desktop_notification(rig) -> None:
    """The per-MR watcher owns that ping; two pings for one merge is noise."""
    _population(rig, _pop(33175, 33176), _pop(33175))
    _lookup(rig, {"33176": "merged"})
    _, state = feed.poll({}, CTX)
    events, _ = feed.poll(state, CTX)
    assert "notify_title" not in events[0]
    assert "notify_message" not in events[0]


def test_mr_left_feed_does_notify_because_no_one_else_reports_it(rig) -> None:
    _population(rig, _pop(33175, 33176), _pop(33175))
    _lookup(rig, {"33176": "opened"})
    _, state = feed.poll({}, CTX)
    events, _ = feed.poll(state, CTX)
    assert events[0]["notify_title"] == "!33176 left the feed"


def test_a_departed_iid_leaves_the_known_set(rig) -> None:
    _population(rig, _pop(33175, 33176), _pop(33175))
    _lookup(rig, {"33176": "merged"})
    _, state = feed.poll({}, CTX)
    _, state = feed.poll(state, CTX)
    assert list(state["known"]) == ["33175"]


# ---------------------------------------------------------------------------
# failure — an unreachable GitLab must not read as "everything vanished"
# ---------------------------------------------------------------------------

def test_a_failed_fetch_emits_nothing_and_keeps_the_known_set(rig) -> None:
    _population(rig, _pop(33175, 33176))
    _, state = feed.poll({}, CTX)
    rig["monkeypatch"].setattr(feed, "fetch_population", lambda _s: None)
    events, new_state = feed.poll(state, CTX)
    assert events == []
    assert sorted(new_state["known"]) == ["33175", "33176"]


def test_a_failed_fetch_does_not_stop_any_watcher(rig) -> None:
    _population(rig, _pop(33175, 33176))
    _, state = feed.poll({}, CTX)
    rig["monkeypatch"].setattr(feed, "fetch_population", lambda _s: None)
    feed.poll(state, CTX)
    assert rig["calls"]["stopped"] == []


# ---------------------------------------------------------------------------
# fetch_population — one list call, no pipeline enrichment
# ---------------------------------------------------------------------------

def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["glab"], returncode, stdout, stderr)


def test_fetch_population_queries_the_resolved_filter(monkeypatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(feed.mrs, "_run",
                        lambda cmd, timeout=25: (seen.append(cmd), _completed(0, "[]"))[1])
    assert feed.fetch_population("@me") == {}
    assert "--author" in seen[0] and "@me" in seen[0]
    assert "--merged" not in seen[0] and "--all" not in seen[0]


def test_fetch_population_is_one_call_and_skips_pipeline_enrichment(monkeypatch) -> None:
    """Discovery needs to know an MR exists, not what its pipeline is doing —
    that is the per-MR watcher's job, and it is the expensive half."""
    calls: list[list[str]] = []
    enriched: list[int] = []
    payload = '[{"iid": 33176, "title": "t", "web_url": "u"}]'
    monkeypatch.setattr(feed.mrs, "_run",
                        lambda cmd, timeout=25: (calls.append(cmd), _completed(0, payload))[1])
    monkeypatch.setattr(feed.mrs, "_enrich", lambda *a, **k: enriched.append(1))
    assert feed.fetch_population("@me") == {"33176": {"title": "t", "web_url": "u"}}
    assert len(calls) == 1
    assert enriched == []


def test_fetch_population_returns_none_on_a_glab_error(monkeypatch) -> None:
    monkeypatch.setattr(feed.mrs, "_run",
                        lambda *a, **k: _completed(1, "", "401 Unauthorized"))
    assert feed.fetch_population("@me") is None


def test_fetch_population_returns_none_on_unparseable_output(monkeypatch) -> None:
    monkeypatch.setattr(feed.mrs, "_run", lambda *a, **k: _completed(0, "not json"))
    assert feed.fetch_population("@me") is None


def test_fetch_population_returns_none_when_glab_is_missing(monkeypatch) -> None:
    def _boom(*_a, **_k):
        raise OSError("glab not found")

    monkeypatch.setattr(feed.mrs, "_run", _boom)
    assert feed.fetch_population("@me") is None


def test_lookup_mr_state_reads_the_live_state(monkeypatch) -> None:
    monkeypatch.setattr(feed.mr_op, "_glab_api",
                        lambda ep: _completed(0, '{"state": "closed"}'))
    assert feed.lookup_mr_state("33176") == "closed"


def test_lookup_mr_state_is_empty_when_the_api_fails(monkeypatch) -> None:
    monkeypatch.setattr(feed.mr_op, "_glab_api", lambda ep: _completed(1, "", "boom"))
    assert feed.lookup_mr_state("33176") == ""


def test_lookup_mr_state_is_empty_when_the_body_is_not_an_mr(monkeypatch) -> None:
    """An unreadable answer is not evidence of anything, least of all a merge —
    every unreadable path has to land on mr_left_feed, not mr_merged."""
    monkeypatch.setattr(feed.mr_op, "_glab_api", lambda ep: _completed(0, '["nope"]'))
    assert feed.lookup_mr_state("33176") == ""


def test_lookup_mr_state_is_empty_on_unparseable_output(monkeypatch) -> None:
    monkeypatch.setattr(feed.mr_op, "_glab_api", lambda ep: _completed(0, "not json"))
    assert feed.lookup_mr_state("33176") == ""


def test_lookup_mr_state_is_empty_when_glab_is_missing(monkeypatch) -> None:
    def _boom(_ep):
        raise FileNotFoundError("glab")

    monkeypatch.setattr(feed.mr_op, "_glab_api", _boom)
    assert feed.lookup_mr_state("33176") == ""


# ---------------------------------------------------------------------------
# contract
# ---------------------------------------------------------------------------

def test_the_feed_never_terminates() -> None:
    """A feed that stopped itself would restore the blindness silently."""
    for state in ({}, {"known": {}}, {"known": {"33175": {}}}, {"mr_state": "merged"}):
        assert feed.is_terminal(state) is False


def test_the_feed_polls_on_human_timescales_not_ci_timescales() -> None:
    assert feed.INTERVAL >= 60


def test_declared_events_match_what_poll_can_emit() -> None:
    declared = {e["key"] for e in
                json.loads((SOURCE_DIR / "events.json").read_text())["events"]}
    assert declared == {"mr_opened", "mr_merged", "mr_closed", "mr_left_feed"}


def test_every_default_filtered_event_is_declared_by_this_source() -> None:
    """An only= entry no source emits is a filter that silences everything."""
    declared = {e["key"] for e in
                json.loads((SOURCE_DIR / "events.json").read_text())["events"]}
    assert set(feed.defaults.DEFAULT_FEED_ONLY.split(",")) <= declared
