"""Tests for the github-pr-feed watch source (issue #1780).

`gitlab-mr-feed` already closes this gap on the GitLab side (#422): every
other source polls one known id, so nothing in the running system could
discover a PR that did not exist at spawn time. This source polls the
population instead. The tests pin the two claims that gap turns on -- a new
number gets *that exact number* watched and announced, and a vanished number
is described by what actually happened to it rather than by a guess -- plus
the failure modes that would restore the silence: a fetch failure must not
read as "everything vanished", a repo-target ambiguity must not let a
spawned poller collide with another clone's (#673), and the feed must never
stop itself.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
SOURCE_DIR = REPO / "presets" / "watch" / "sources" / "github-pr-feed"

_spec = importlib.util.spec_from_file_location("gh_pr_feed_poller", SOURCE_DIR / "poller.py")
assert _spec is not None and _spec.loader is not None
feed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(feed)

CTX = {"source": "github-pr-feed", "id": "@open", "only": []}


def _pop(*numbers: int) -> dict[str, dict[str, str]]:
    return {str(n): {"title": f"title {n}", "url": f"https://gh/pr/{n}"} for n in numbers}


def _keys(events: list[dict]) -> list[str]:
    return [e["event"] for e in events]


def _by_key(events: list[dict], key: str) -> dict:
    return next(e for e in events if e["event"] == key)


@pytest.fixture
def rig(monkeypatch):
    """Stub every outward call; record spawns, stops and lookups.

    `spawn_ok` and `only` model the per-PR tier the feed reasons about:
    whether a poller could be started at all, and -- for one already
    running -- which events it is filtered to. Both decide whether a
    terminal transition already has a reporter, so both have to be settable
    per test. `watched=None` models the #673 repo-target ambiguity.
    """
    calls: dict = {"spawned": [], "stopped": [], "looked_up": [],
                   "watched": set(), "spawn_ok": True, "only": {}}
    monkeypatch.setattr(feed, "live_watchers", lambda: calls["watched"])
    monkeypatch.setattr(feed, "spawn_watcher",
                        lambda n: (calls["spawned"].append(n), calls["spawn_ok"])[1])
    monkeypatch.setattr(feed, "stop_watcher", lambda n: calls["stopped"].append(n))
    monkeypatch.setattr(feed, "watcher_only", lambda n: calls["only"].get(n),
                        raising=False)
    return {"calls": calls, "monkeypatch": monkeypatch}


def _population(rig, *pops):
    """Queue one population per poll; the last one repeats."""
    seq = [(p, "") if p is not None else (None, "ERROR: gh could not run")
           for p in pops]

    def _fetch(_scope):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    rig["monkeypatch"].setattr(feed, "fetch_population", _fetch)


def _lookup(rig, mapping):
    def _do(number):
        rig["calls"]["looked_up"].append(number)
        return mapping.get(number, "")

    rig["monkeypatch"].setattr(feed, "lookup_pr_state", _do)


# ---------------------------------------------------------------------------
# scope resolution -- the caller picks the population, `gh-prs`'s own
# vocabulary since #1207 removed the implicit author filter (#1780 point 1)
# ---------------------------------------------------------------------------

def test_the_default_scope_is_the_whole_open_population() -> None:
    assert feed.resolve_filters("@open") == {}


def test_a_known_filter_token_resolves() -> None:
    assert feed.resolve_filters("author=@me") == {"author": "@me"}


def test_an_unknown_token_is_refused_not_widened() -> None:
    """Building the query anyway would discover PRs outside the population
    the caller asked for -- the #939 shape, on this source's own feed."""
    assert feed.resolve_filters("milestone=v19") is None


def test_an_unmappable_value_is_refused_too() -> None:
    assert feed.resolve_filters("state=opne") is None


# ---------------------------------------------------------------------------
# first poll -- baseline, not a notification storm
# ---------------------------------------------------------------------------

def test_first_poll_announces_nothing(rig) -> None:
    """Every PR open when the feed starts is not a discovery."""
    _population(rig, _pop(101, 102))
    events, state = feed.poll({}, CTX)
    assert events == []
    assert sorted(state["known"]) == ["101", "102"]


def test_first_poll_still_covers_unwatched_prs(rig) -> None:
    _population(rig, _pop(101, 102))
    rig["calls"]["watched"] = {"102"}
    feed.poll({}, CTX)
    assert rig["calls"]["spawned"] == ["101"]


# ---------------------------------------------------------------------------
# discovery -- the gap #1780 exists to close
# ---------------------------------------------------------------------------

def test_a_new_number_is_watched_and_announced(rig) -> None:
    """#103 opened between two radar runs, invisible until the second one.
    It must now surface with no one typing anything."""
    _population(rig, _pop(101), _pop(101, 103))
    _, state = feed.poll({}, CTX)
    rig["calls"]["watched"] = {"101"}
    rig["calls"]["spawned"].clear()
    events, state = feed.poll(state, CTX)
    assert rig["calls"]["spawned"] == ["103"]
    assert _keys(events) == ["pr_opened"]
    assert _by_key(events, "pr_opened")["payload"]["number"] == "103"


def test_pr_opened_carries_the_title_and_url(rig) -> None:
    _population(rig, _pop(101), _pop(101, 103))
    _, state = feed.poll({}, CTX)
    events, _ = feed.poll(state, CTX)
    payload = _by_key(events, "pr_opened")["payload"]
    assert payload["title"] == "title 103"
    assert payload["url"] == "https://gh/pr/103"


def test_pr_opened_notifies_because_nothing_else_can_report_it(rig) -> None:
    _population(rig, _pop(101), _pop(101, 103))
    _, state = feed.poll({}, CTX)
    events, _ = feed.poll(state, CTX)
    ev = _by_key(events, "pr_opened")
    assert ev["notify_title"] == "#103 opened"
    assert ev["notify_message"] == "title 103"


def test_an_unchanged_population_announces_nothing(rig) -> None:
    _population(rig, _pop(101, 102))
    _, state = feed.poll({}, CTX)
    rig["calls"]["watched"] = {"101", "102"}
    events, _ = feed.poll(state, CTX)
    assert events == []
    assert rig["calls"]["spawned"] == ["101", "102"]


def test_a_known_pr_whose_watcher_died_is_recovered(rig) -> None:
    """Coverage is continuous for the same reason discovery is."""
    _population(rig, _pop(101, 102))
    _, state = feed.poll({}, CTX)
    rig["calls"]["watched"] = {"101"}
    rig["calls"]["spawned"].clear()
    feed.poll(state, CTX)
    assert rig["calls"]["spawned"] == ["102"]


# ---------------------------------------------------------------------------
# repo-target ambiguity (#673) -- coverage unknown must never be guessed shut
# ---------------------------------------------------------------------------

def test_watched_none_spawns_nothing(rig) -> None:
    """A poller spawned here for #N would be indistinguishable from #N of
    whatever clone started it (#673). Discovery still works; healing does not."""
    _population(rig, _pop(101), _pop(101, 103))
    _, state = feed.poll({}, CTX)
    rig["calls"]["watched"] = None
    rig["calls"]["spawned"].clear()
    events, _ = feed.poll(state, CTX)
    assert rig["calls"]["spawned"] == []
    assert _keys(events) == ["pr_opened"]


def test_watched_none_still_reports_a_departure_conservatively(rig) -> None:
    """Coverage unknown must resolve to *not covered*, never to *covered by
    someone I cannot name* -- the fallback is a possible duplicate, which is
    visible and cheap; the other direction is silence. Coverage has to be
    unknown from the *first* poll here: a poll that could spawn (and
    therefore knows it covered #102 itself) is a different, legitimate
    suppression case, pinned separately above."""
    rig["calls"]["watched"] = None
    _population(rig, _pop(101, 102), _pop(101))
    _lookup(rig, {"102": "MERGED"})
    _, state = feed.poll({}, CTX)
    events, _ = feed.poll(state, CTX)
    assert _keys(events) == ["pr_merged"]


# ---------------------------------------------------------------------------
# departure -- vanished is not the same claim as merged
# ---------------------------------------------------------------------------

def test_a_vanished_number_that_merged_reports_merged_and_stops_its_watcher(rig) -> None:
    """No per-PR poller could be started for it, so the feed is the only tier
    that can report the merge -- and the only tier that has to clean up."""
    rig["calls"]["spawn_ok"] = False
    _population(rig, _pop(101, 103), _pop(101))
    _lookup(rig, {"103": "MERGED"})
    _, state = feed.poll({}, CTX)
    events, _ = feed.poll(state, CTX)
    assert _keys(events) == ["pr_merged"]
    assert rig["calls"]["stopped"] == ["103"]


def test_a_vanished_number_that_closed_is_not_reported_as_merged(rig) -> None:
    """Inventing pr_merged for a closed PR is the confident-wrong class."""
    rig["calls"]["spawn_ok"] = False
    _population(rig, _pop(101, 103), _pop(101))
    _lookup(rig, {"103": "CLOSED"})
    _, state = feed.poll({}, CTX)
    events, _ = feed.poll(state, CTX)
    assert _keys(events) == ["pr_closed"]
    assert rig["calls"]["looked_up"] == ["103"]


def test_a_vanished_number_still_open_is_reported_honestly_and_kept_watched(rig) -> None:
    """Reassigned, or the filter changed. Neither is an ending, and following
    a PR the feed no longer returns is legitimate."""
    _population(rig, _pop(101, 103), _pop(101))
    _lookup(rig, {"103": "OPEN"})
    _, state = feed.poll({}, CTX)
    events, _ = feed.poll(state, CTX)
    assert _keys(events) == ["pr_left_feed"]
    assert _by_key(events, "pr_left_feed")["payload"]["pr_state"] == "OPEN"
    assert rig["calls"]["stopped"] == []


def test_a_vanished_number_that_cannot_be_looked_up_says_unknown(rig) -> None:
    _population(rig, _pop(101, 103), _pop(101))
    _lookup(rig, {})
    _, state = feed.poll({}, CTX)
    events, _ = feed.poll(state, CTX)
    assert _by_key(events, "pr_left_feed")["payload"]["pr_state"] == "unknown"
    assert rig["calls"]["stopped"] == []


def test_merged_and_closed_do_not_fire_a_desktop_notification(rig) -> None:
    """Even when the feed is the sole reporter it stays off the notification
    centre: the terminal ping belongs to the per-PR tier wherever it exists."""
    rig["calls"]["spawn_ok"] = False
    _population(rig, _pop(101, 103), _pop(101))
    _lookup(rig, {"103": "MERGED"})
    _, state = feed.poll({}, CTX)
    events, _ = feed.poll(state, CTX)
    assert "notify_title" not in events[0]
    assert "notify_message" not in events[0]


def test_pr_left_feed_does_notify_because_no_one_else_reports_it(rig) -> None:
    _population(rig, _pop(101, 103), _pop(101))
    _lookup(rig, {"103": "OPEN"})
    _, state = feed.poll({}, CTX)
    events, _ = feed.poll(state, CTX)
    assert events[0]["notify_title"] == "#103 left the feed"


def test_a_departed_number_leaves_the_known_set(rig) -> None:
    _population(rig, _pop(101, 103), _pop(101))
    _lookup(rig, {"103": "MERGED"})
    _, state = feed.poll({}, CTX)
    _, state = feed.poll(state, CTX)
    assert list(state["known"]) == ["101"]


# ---------------------------------------------------------------------------
# suppression -- must fire and must not fire, paired (the pair the brief asks
# for: an untested "must not fire" is untested by construction)
# ---------------------------------------------------------------------------

def test_a_merge_its_own_poller_reports_is_not_announced_twice(rig) -> None:
    """The per-PR poller is unfiltered (spawned with only=[]), so it owes the
    `merged` event itself -- the feed saying it again would be one fact
    rendered as two lines."""
    _population(rig, _pop(101, 103), _pop(101))
    _lookup(rig, {"103": "MERGED"})
    _, state = feed.poll({}, CTX)
    events, _ = feed.poll(state, CTX)
    assert _keys(events) == []


def test_the_feed_still_reports_a_merge_nothing_else_covers(rig) -> None:
    """The pin that stops the suppression above becoming a coverage hole.
    With no per-PR poller behind it the feed is the only tier that can say
    the PR ended, and a suppressed event here is not one clean line -- it is
    silence."""
    rig["calls"]["spawn_ok"] = False
    _population(rig, _pop(101, 103), _pop(101))
    _lookup(rig, {"103": "MERGED"})
    _, state = feed.poll({}, CTX)
    events, _ = feed.poll(state, CTX)
    assert _keys(events) == ["pr_merged"]
    assert _by_key(events, "pr_merged")["payload"]["number"] == "103"


def test_a_live_poller_filtered_away_from_merged_does_not_buy_silence(rig) -> None:
    """The case that sinks suppressing on liveness alone: this poller is
    alive and healthy and will never say a word about the merge, because
    `merged` is not in its filter. Trusting its existence loses the event
    outright."""
    rig["calls"]["watched"] = {"101", "103"}
    rig["calls"]["only"] = {"103": ["checks_failed", "checks_succeeded"]}
    _population(rig, _pop(101, 103), _pop(101))
    _lookup(rig, {"103": "MERGED"})
    _, state = feed.poll({}, CTX)
    events, _ = feed.poll(state, CTX)
    assert rig["calls"]["spawned"] == []
    assert _keys(events) == ["pr_merged"]


def test_an_unreadable_watcher_filter_reports_rather_than_stays_silent(rig) -> None:
    """Unknown resolves to not-covered. The fallback is a duplicate, which is
    visible and cheap; the other direction is a radar that stops reporting."""
    rig["calls"]["watched"] = {"101", "103"}
    rig["calls"]["only"] = {}
    _population(rig, _pop(101, 103), _pop(101))
    _lookup(rig, {"103": "MERGED"})
    _, state = feed.poll({}, CTX)
    events, _ = feed.poll(state, CTX)
    assert _keys(events) == ["pr_merged"]


# ---------------------------------------------------------------------------
# failure -- an unreachable GitHub must not read as "everything vanished"
# ---------------------------------------------------------------------------

def test_a_failed_fetch_fires_no_departure_and_keeps_the_known_set(rig) -> None:
    """The event it *does* fire is `prs_unreachable`. What must not appear
    here is a departure: an outage read as an empty population announces
    every PR you have as gone, and then as new again on recovery."""
    _population(rig, _pop(101, 103))
    _, state = feed.poll({}, CTX)
    rig["monkeypatch"].setattr(feed, "fetch_population",
                               lambda _s: (None, "ERROR: gh could not run"))
    events, new_state = feed.poll(state, CTX)
    assert [e["event"] for e in events] == ["prs_unreachable"]
    assert sorted(new_state["known"]) == ["101", "103"]


def test_a_failed_fetch_does_not_stop_any_watcher(rig) -> None:
    _population(rig, _pop(101, 103))
    _, state = feed.poll({}, CTX)
    rig["monkeypatch"].setattr(feed, "fetch_population",
                               lambda _s: (None, "ERROR: gh could not run"))
    feed.poll(state, CTX)
    assert rig["calls"]["stopped"] == []


def test_the_outage_event_fires_once_not_every_poll(rig) -> None:
    _population(rig, _pop(101))
    _, state = feed.poll({}, CTX)
    rig["monkeypatch"].setattr(feed, "fetch_population",
                               lambda _s: (None, "ERROR: gh could not run"))
    events1, state = feed.poll(state, CTX)
    events2, _ = feed.poll(state, CTX)
    assert [e["event"] for e in events1] == ["prs_unreachable"]
    assert events2 == []


# ---------------------------------------------------------------------------
# fetch_population -- one gh pr list call, no check-rollup enrichment
# ---------------------------------------------------------------------------

def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["gh"], returncode, stdout, stderr)


def test_fetch_population_queries_the_resolved_filter(monkeypatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(feed.subprocess, "run",
                        lambda cmd, **k: (seen.append(cmd), _completed(0, "[]"))[1])
    assert feed.fetch_population("author=@me") == ({}, "")
    assert "--author" in seen[0] and "@me" in seen[0]


def test_fetch_population_parses_number_title_url(monkeypatch) -> None:
    payload = json.dumps([{"number": 103, "title": "t", "url": "u"}])
    monkeypatch.setattr(feed.subprocess, "run",
                        lambda cmd, **k: _completed(0, payload))
    assert feed.fetch_population("@open") == ({"103": {"title": "t", "url": "u"}}, "")


def test_fetch_population_returns_none_on_a_gh_error(monkeypatch) -> None:
    monkeypatch.setattr(feed.subprocess, "run",
                        lambda cmd, **k: _completed(1, "", "401 Unauthorized"))
    assert feed.fetch_population("@open")[0] is None


def test_fetch_population_returns_none_on_unparseable_output(monkeypatch) -> None:
    monkeypatch.setattr(feed.subprocess, "run",
                        lambda cmd, **k: _completed(0, "not json"))
    assert feed.fetch_population("@open")[0] is None


def test_fetch_population_returns_none_when_gh_is_missing(monkeypatch) -> None:
    def _boom(*_a, **_k):
        raise FileNotFoundError("gh not found")

    monkeypatch.setattr(feed.subprocess, "run", _boom)
    assert feed.fetch_population("@open")[0] is None


def test_fetch_population_refuses_an_unknown_scope_token_with_a_reason(monkeypatch) -> None:
    pop, error = feed.fetch_population("milestone=v19")
    assert pop is None
    assert "milestone" in error


def test_lookup_pr_state_reads_the_live_state(monkeypatch) -> None:
    monkeypatch.setattr(feed, "_gh", lambda args: _completed(0, '{"state": "MERGED"}'))
    assert feed.lookup_pr_state("103") == "MERGED"


def test_lookup_pr_state_is_empty_when_the_call_fails(monkeypatch) -> None:
    monkeypatch.setattr(feed, "_gh", lambda args: _completed(1, "", "boom"))
    assert feed.lookup_pr_state("103") == ""


def test_lookup_pr_state_is_empty_on_unparseable_output(monkeypatch) -> None:
    monkeypatch.setattr(feed, "_gh", lambda args: _completed(0, "not json"))
    assert feed.lookup_pr_state("103") == ""


def test_lookup_pr_state_is_empty_when_gh_is_missing(monkeypatch) -> None:
    def _boom(_args):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(feed, "_gh", _boom)
    assert feed.lookup_pr_state("103") == ""


# --- the coverage probe itself ---------------------------------------------

def test_watcher_only_reads_the_filter_off_the_state_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(feed.transport, "STATE_DIR", str(tmp_path))
    Path(feed.transport.state_path("github-pr", "103")).write_text(
        json.dumps({"only": ["merged", "closed"]}), encoding="utf-8")
    assert feed.watcher_only("103") == ["merged", "closed"]


def test_watcher_only_is_none_when_nothing_recorded_a_filter(monkeypatch, tmp_path) -> None:
    """None and [] are different answers: [] is "emits everything", None is
    "nobody could tell us", and only one of them may buy silence."""
    monkeypatch.setattr(feed.transport, "STATE_DIR", str(tmp_path))
    assert feed.watcher_only("103") is None
    Path(feed.transport.state_path("github-pr", "104")).write_text(
        json.dumps({"source_state": {}}), encoding="utf-8")
    assert feed.watcher_only("104") is None


def test_an_unfiltered_poller_covers_every_terminal_event(rig) -> None:
    rig["calls"]["only"] = {"103": []}
    assert feed.terminal_coverage("103", {"103"}) == ["merged", "closed"]


def test_a_number_with_no_live_poller_has_no_coverage(rig) -> None:
    rig["calls"]["only"] = {"103": ["merged", "closed"]}
    assert feed.terminal_coverage("103", set()) == []


def test_a_freshly_spawned_poller_covers_everything(rig) -> None:
    """The feed spawns per-PR watchers with an empty filter (see
    `spawn_watcher`), so it knows the answer without waiting a tick for the
    new poller to write its own state file."""
    assert feed.terminal_coverage("103", set(), spawned=True) == ["merged", "closed"]


# ---------------------------------------------------------------------------
# contract
# ---------------------------------------------------------------------------

def test_the_feed_never_terminates() -> None:
    """A feed that stopped itself would restore the blindness silently."""
    for state in ({}, {"known": {}}, {"known": {"101": {}}}, {"pr_state": "MERGED"}):
        assert feed.is_terminal(state) is False


def test_the_feed_polls_on_human_timescales_not_ci_timescales() -> None:
    assert feed.INTERVAL >= 60


def test_declared_events_match_what_poll_can_emit() -> None:
    declared = {e["key"] for e in
                json.loads((SOURCE_DIR / "events.json").read_text(encoding="utf-8"))["events"]}
    assert declared == {"pr_opened", "pr_merged", "pr_closed", "pr_left_feed",
                        "prs_unreachable"}
    assert declared == set(feed.EVENT_KEYS)
