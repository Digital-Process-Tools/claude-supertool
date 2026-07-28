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
    """Stub every outward call; record spawns, stops and lookups.

    `spawn_ok` and `only` model the per-MR tier the feed now reasons about:
    whether a poller could be started at all, and — for one already running —
    which events it is filtered to. Both decide whether a terminal transition
    already has a reporter, so both have to be settable per test.
    """
    calls: dict = {"spawned": [], "stopped": [], "looked_up": [], "watched": set(),
                   "spawn_ok": True, "only": {}}
    monkeypatch.setattr(feed, "live_watchers", lambda: set(calls["watched"]))
    monkeypatch.setattr(feed, "spawn_watcher",
                        lambda iid: (calls["spawned"].append(iid), calls["spawn_ok"])[1])
    monkeypatch.setattr(feed, "stop_watcher", lambda iid: calls["stopped"].append(iid))
    monkeypatch.setattr(feed, "watcher_only", lambda iid: calls["only"].get(iid),
                        raising=False)
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
    """No per-MR poller could be started for it, so the feed is the only tier
    that can report the merge — and the only tier that has to clean up."""
    rig["calls"]["spawn_ok"] = False
    _population(rig, _pop(33175, 33176), _pop(33175))
    _lookup(rig, {"33176": "merged"})
    _, state = feed.poll({}, CTX)
    events, _ = feed.poll(state, CTX)
    assert _keys(events) == ["mr_merged"]
    assert rig["calls"]["stopped"] == ["33176"]


def test_a_vanished_iid_that_closed_is_not_reported_as_merged(rig) -> None:
    """Inventing mr_merged for a closed MR is the confident-wrong class."""
    rig["calls"]["spawn_ok"] = False
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
    """Even when the feed is the sole reporter it stays off the notification
    centre: the terminal ping belongs to the per-MR tier wherever it exists."""
    rig["calls"]["spawn_ok"] = False
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


# ---------------------------------------------------------------------------
# multi-author scopes (issue #425)
#
# The feed is the third view of radar's population. A scope radar can express
# but the feed cannot poll would put discovery back out of step with the
# board — the same silent incompleteness, one tier down.
# ---------------------------------------------------------------------------

class _Result:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


def _glab(monkeypatch, by_author: dict[str, list[int]], fail_for: tuple = ()) -> list[str]:
    """Serve `glab mr list` from a per-author map. Returns the author log."""
    seen: list[str] = []

    def _run(cmd, timeout=25):
        author = cmd[cmd.index("--author") + 1] if "--author" in cmd else ""
        seen.append(author)
        if author in fail_for:
            return _Result("", returncode=1)
        return _Result(json.dumps([
            {"iid": i, "title": f"title {i}", "web_url": f"https://gl/mr/{i}"}
            for i in by_author.get(author, [])
        ]))

    monkeypatch.setattr(feed.mrs, "_run", _run)
    return seen


def test_a_two_author_scope_fans_out_and_unions(monkeypatch) -> None:
    seen = _glab(monkeypatch, {"@me": [1, 2], "modular.system": [2, 3]})
    pop = feed.fetch_population("author=@me,author=modular.system,state=opened")
    assert seen == ["@me", "modular.system"]
    assert sorted(pop) == ["1", "2", "3"]


def test_a_single_author_scope_still_makes_exactly_one_call(monkeypatch) -> None:
    seen = _glab(monkeypatch, {"@me": [1]})
    assert sorted(feed.fetch_population("@me")) == ["1"]
    assert seen == ["@me"]


def test_one_failing_query_fails_the_whole_poll(monkeypatch) -> None:
    """Half a population is worse than none: every iid the missing query would
    have returned reads as a departure and fires an event saying so."""
    _glab(monkeypatch, {"@me": [1]}, fail_for=("modular.system",))
    assert feed.fetch_population("author=@me,author=modular.system,state=opened") is None


# ---------------------------------------------------------------------------
# double-reporting (issue #434)
#
# The feed and the per-MR pollers are two independent layers over one fact, so
# a merge arrived twice — `merged` from the poller, `mr_merged` from the feed,
# seconds apart under different event keys. Duplicates train the reader to skim
# the board, which is how a real red gets missed.
#
# The feed suppresses its own terminal event only when a per-MR poller for that
# iid *announces that transition itself*. Liveness at emit time cannot answer
# that: reporting `merged` is precisely what makes a per-MR poller terminal and
# kill itself, so by the time the feed notices the departure the reporter is
# already gone — and when one IS still alive, it is the one that has not spoken
# yet. So the feed records coverage per iid while the MR is still open, and
# every unanswerable question resolves to "not covered", i.e. to a duplicate
# rather than to silence.
# ---------------------------------------------------------------------------

def test_a_merge_its_own_poller_reports_is_not_announced_twice(rig) -> None:
    """The defect. !33180 merged once and the board showed two lines for it."""
    _population(rig, _pop(33175, 33180), _pop(33175))
    _lookup(rig, {"33180": "merged"})
    _, state = feed.poll({}, CTX)          # spawns the per-MR poller for 33180
    events, _ = feed.poll(state, CTX)
    assert _keys(events) == []


def test_a_close_its_own_poller_reports_is_not_announced_twice(rig) -> None:
    _population(rig, _pop(33175, 33180), _pop(33175))
    _lookup(rig, {"33180": "closed"})
    _, state = feed.poll({}, CTX)
    events, _ = feed.poll(state, CTX)
    assert _keys(events) == []


def test_the_feed_still_reports_a_merge_nothing_else_covers(rig) -> None:
    """The pin that stops this fix becoming a coverage hole. With no per-MR
    poller behind it the feed is the only tier that can say the MR ended, and
    a suppressed event here is not one clean line — it is silence."""
    rig["calls"]["spawn_ok"] = False
    _population(rig, _pop(33175, 33180), _pop(33175))
    _lookup(rig, {"33180": "merged"})
    _, state = feed.poll({}, CTX)
    events, _ = feed.poll(state, CTX)
    assert _keys(events) == ["mr_merged"]
    assert _by_key(events, "mr_merged")["payload"]["iid"] == "33180"


def test_a_live_poller_filtered_away_from_merged_does_not_buy_silence(rig) -> None:
    """The case that sinks suppressing on liveness alone: this poller is alive
    and healthy and will never say a word about the merge, because `merged` is
    not in its filter. Trusting its existence loses the event outright."""
    rig["calls"]["watched"] = {"33175", "33180"}
    rig["calls"]["only"] = {"33180": ["pipeline_failed", "pipeline_succeeded"]}
    _population(rig, _pop(33175, 33180), _pop(33175))
    _lookup(rig, {"33180": "merged"})
    _, state = feed.poll({}, CTX)
    events, _ = feed.poll(state, CTX)
    assert rig["calls"]["spawned"] == []
    assert _keys(events) == ["mr_merged"]


def test_an_unreadable_watcher_filter_reports_rather_than_stays_silent(rig) -> None:
    """Unknown resolves to not-covered. The fallback is a duplicate, which is
    visible and cheap; the other direction is a radar that stops reporting."""
    rig["calls"]["watched"] = {"33175", "33180"}
    rig["calls"]["only"] = {}              # nothing recorded what it will emit
    _population(rig, _pop(33175, 33180), _pop(33175))
    _lookup(rig, {"33180": "merged"})
    _, state = feed.poll({}, CTX)
    events, _ = feed.poll(state, CTX)
    assert _keys(events) == ["mr_merged"]


def test_a_covered_iid_never_suppresses_another_iids_event(rig) -> None:
    """Two MRs in one merge wave, one covered and one not. Suppression keyed on
    anything coarser than the iid silences the MR nobody else is reporting."""
    rig["calls"]["watched"] = {"33180", "33181"}
    rig["calls"]["only"] = {"33180": ["merged", "closed"],
                            "33181": ["pipeline_failed"]}
    _population(rig, _pop(33175, 33180, 33181), _pop(33175))
    _lookup(rig, {"33180": "merged", "33181": "merged"})
    _, state = feed.poll({}, CTX)
    events, _ = feed.poll(state, CTX)
    assert _keys(events) == ["mr_merged"]
    assert _by_key(events, "mr_merged")["payload"]["iid"] == "33181"


def test_the_reporting_poller_is_not_killed_before_it_reports(rig) -> None:
    """`stop_watcher` SIGTERMs then SIGKILLs. Suppressing the feed event while
    killing the poller that owed us the event is how one duplicate becomes no
    report at all — the exact hole this fix must not open."""
    rig["calls"]["watched"] = {"33175", "33180"}
    rig["calls"]["only"] = {"33180": ["merged", "closed"]}
    _population(rig, _pop(33175, 33180), _pop(33175))
    _lookup(rig, {"33180": "merged"})
    _, state = feed.poll({}, CTX)
    events, _ = feed.poll(state, CTX)
    assert _keys(events) == []
    assert rig["calls"]["stopped"] == []


def test_an_uncovered_departure_still_cleans_up_its_watcher(rig) -> None:
    """The stale-PID sweep the unwatch was there for stays on the path where
    nothing else is going to run."""
    rig["calls"]["spawn_ok"] = False
    _population(rig, _pop(33175, 33180), _pop(33175))
    _lookup(rig, {"33180": "merged"})
    _, state = feed.poll({}, CTX)
    feed.poll(state, CTX)
    assert rig["calls"]["stopped"] == ["33180"]


def test_a_still_open_departure_is_untouched_by_the_dedup(rig) -> None:
    """mr_left_feed has no per-MR twin — no source emits it but this one."""
    rig["calls"]["watched"] = {"33175", "33180"}
    rig["calls"]["only"] = {"33180": ["merged", "closed"]}
    _population(rig, _pop(33175, 33180), _pop(33175))
    _lookup(rig, {"33180": "opened"})
    _, state = feed.poll({}, CTX)
    events, _ = feed.poll(state, CTX)
    assert _keys(events) == ["mr_left_feed"]


def test_a_state_file_written_before_this_fix_reports_rather_than_silences(rig) -> None:
    """An in-flight feed upgraded mid-session has `known` entries with no
    coverage recorded. Missing must read as not-covered."""
    _lookup(rig, {"33180": "merged"})
    _population(rig, _pop(33175))
    legacy = {"scope": "@me", "known": {
        "33175": {"title": "t", "web_url": "u"},
        "33180": {"title": "t", "web_url": "u"},
    }}
    events, _ = feed.poll(legacy, CTX)
    assert _keys(events) == ["mr_merged"]


# --- the coverage probe itself ---------------------------------------------

def test_watcher_only_reads_the_filter_off_the_state_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(feed.transport, "STATE_DIR", str(tmp_path))
    Path(feed.transport.state_path("gitlab-mr", "33180")).write_text(
        json.dumps({"only": ["merged", "closed"]}), encoding="utf-8")
    assert feed.watcher_only("33180") == ["merged", "closed"]


def test_watcher_only_is_none_when_nothing_recorded_a_filter(monkeypatch, tmp_path) -> None:
    """None and [] are different answers: [] is "emits everything", None is
    "nobody could tell us", and only one of them may buy silence."""
    monkeypatch.setattr(feed.transport, "STATE_DIR", str(tmp_path))
    assert feed.watcher_only("33180") is None
    Path(feed.transport.state_path("gitlab-mr", "33181")).write_text(
        json.dumps({"source_state": {}}), encoding="utf-8")
    assert feed.watcher_only("33181") is None


def test_an_unfiltered_poller_covers_every_terminal_event(rig) -> None:
    rig["calls"]["only"] = {"33180": []}
    assert feed.terminal_coverage("33180", {"33180"}) == ["merged", "closed"]


def test_an_iid_with_no_live_poller_has_no_coverage(rig) -> None:
    rig["calls"]["only"] = {"33180": ["merged", "closed"]}
    assert feed.terminal_coverage("33180", set()) == []


def test_a_freshly_spawned_poller_covers_the_shared_default_filter(rig) -> None:
    """The feed spawns with DEFAULT_ONLY, so it knows the answer without
    waiting a tick for the new poller to write its state file."""
    covered = feed.terminal_coverage("33180", set(), spawned=True)
    assert covered == [e for e in ("merged", "closed")
                       if e in feed.defaults.DEFAULT_ONLY.split(",")]
    assert covered == ["merged", "closed"]
