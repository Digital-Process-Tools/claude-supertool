"""#1602 — `gitlab-mr-feed` swallowed a failed population fetch.

`poll` ended its failure path on `return [], state`, so an unreachable GitLab,
an expired token or a scope carrying an unknown filter token rendered exactly
like a feed correctly reporting that nothing changed — alive, green in
`watches`, and silent forever. A feed's healthy steady state *is* silence,
which is the one surface where this house defect is hardest to notice.

The contract copied here is `github-issue-feed`'s (#1599), which is the
feed-shaped version of the per-source work in #541:

  * `fetch_population` returns `(pop, "")` or `(None, why)` — the reason
    survives instead of collapsing to `None`.
  * The first failure of a streak emits one `mrs_unreachable`, edge-triggered
    on a `lookup` flag in state, so an outage is announced once rather than
    every five minutes. A signal that repeats is a signal people mute, and a
    muted signal is the original silence by a longer route.
  * `known` survives the outage untouched, so recovery does not announce the
    whole population as fresh arrivals.
  * `radar` gets its own row for it: a feed that could not establish its
    population is a different fact from a feed with nothing to report, and a
    third fact from a poller that crashed.

Nothing here passes against a poller that does nothing: every test names the
event key, the surviving state, or the warning line.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WATCH_DIR = REPO / "presets" / "watch"
SOURCE_DIR = WATCH_DIR / "sources" / "gitlab-mr-feed"
for _dir in (str(WATCH_DIR), str(REPO / "presets"), str(REPO / "tests")):
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

from _changelog_findable import assert_change_is_findable  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


feed = _load("mr_feed_poller_1602", SOURCE_DIR / "poller.py")
defaults = _load("watch_defaults_1602", WATCH_DIR / "defaults.py")

CTX = {"source": "gitlab-mr-feed", "id": "@me", "only": []}
OUTAGE = "ERROR: glab not authenticated. Run: glab auth login"


def _pop(*iids: int) -> dict[str, dict[str, str]]:
    return {str(i): {"title": f"title {i}", "web_url": f"https://gl/mr/{i}"}
            for i in iids}


def _keys(events: list[dict]) -> list[str]:
    return [e["event"] for e in events]


@pytest.fixture
def rig(monkeypatch):
    """Stub every outward call. `stopped` is load-bearing: the defect class
    next door is an outage read as "every MR you had is gone"."""
    calls: dict = {"spawned": [], "stopped": [], "watched": set()}
    monkeypatch.setattr(feed, "live_watchers", lambda: set(calls["watched"]))
    monkeypatch.setattr(feed, "spawn_watcher",
                        lambda iid: (calls["spawned"].append(iid), True)[1])
    monkeypatch.setattr(feed, "stop_watcher", lambda iid: calls["stopped"].append(iid))
    monkeypatch.setattr(feed, "watcher_only", lambda iid: None, raising=False)
    monkeypatch.setattr(feed, "lookup_mr_state", lambda iid: "")
    return {"calls": calls, "monkeypatch": monkeypatch}


def _feed_returns(rig, *answers):
    """Queue one fetch answer per poll; the last one repeats.

    A `dict` is a population, `None` is an outage carrying OUTAGE as its
    reason.
    """
    seq = [(a, "") if a is not None else (None, OUTAGE) for a in answers]

    def _fetch(_scope):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    rig["monkeypatch"].setattr(feed, "fetch_population", _fetch)


def _run(rig, *answers):
    _feed_returns(rig, *answers)
    state: dict = {}
    out = []
    for _ in answers:
        events, state = feed.poll(state, CTX)
        out.append((events, state))
    return out


# --- 1. the outage announces itself -----------------------------------------

def test_an_outage_is_announced_rather_than_rendered_as_silence(rig):
    (_, _), (events, _) = _run(rig, _pop(33175, 33176), None)
    assert _keys(events) == ["mrs_unreachable"]
    payload = events[0]["payload"]
    assert payload["error"] == OUTAGE
    assert payload["scope"] == "@me"
    # `last_known_`, not a bare count: this tick read nothing, so the number
    # describes the last poll that could see rather than GitLab right now.
    assert payload["last_known_count"] == 2


def test_the_outage_event_is_announced_once_per_outage_not_once_per_poll(rig):
    (_, _), (first, _), (second, _), (third, _) = _run(
        rig, _pop(33175), None, None, None)
    assert _keys(first) == ["mrs_unreachable"]
    assert second == []
    assert third == []


def test_a_second_outage_after_a_recovery_is_announced_again(rig):
    """Edge-triggered, not once-ever: the flag has to clear on a good poll."""
    (_, _), (_, _), (_, _), (again, _) = _run(
        rig, _pop(33175), None, _pop(33175), None)
    assert _keys(again) == ["mrs_unreachable"]


# --- 2. an unestablished population is not an empty one ---------------------

def test_an_outage_fires_no_departure_and_keeps_the_known_set(rig):
    (_, first_state), (events, state) = _run(rig, _pop(33175, 33176), None)
    assert "mr_merged" not in _keys(events)
    assert "mr_closed" not in _keys(events)
    assert "mr_left_feed" not in _keys(events)
    assert state["known"] == first_state["known"]
    assert rig["calls"]["stopped"] == []


def test_recovery_does_not_re_announce_the_whole_population(rig):
    """The trap specific to a feed: treat an outage as an empty population and
    every member comes back as a fresh arrival when GitLab does."""
    (_, _), (_, _), (events, _) = _run(rig, _pop(33175, 33176), None,
                                       _pop(33175, 33176))
    assert events == []


def test_an_mr_that_really_did_leave_during_an_outage_is_still_reported(rig):
    """Carrying state forward must not swallow a transition that happened
    while we were blind — that would be the loud bug traded for the quiet one."""
    (_, _), (_, _), (events, _) = _run(rig, _pop(33175, 33176), None, _pop(33175))
    assert _keys(events) == ["mr_left_feed"]


def test_the_lookup_flag_is_cleared_by_a_successful_poll(rig):
    (_, _), (_, blind), (_, good) = _run(rig, _pop(33175), None, _pop(33175))
    assert blind["lookup"] == feed.LOOKUP_UNAVAILABLE
    assert good["lookup"] == feed.LOOKUP_OK
    assert not good.get("error")


def test_an_outage_on_the_very_first_poll_says_so(rig):
    """A feed whose first poll fails has no baseline and no population. It
    still has to say it could not look, or it starts life silent."""
    (events, state), = _run(rig, None)
    assert _keys(events) == ["mrs_unreachable"]
    assert events[0]["payload"]["last_known_count"] == 0
    assert not state.get("known")


# --- 3. fetch_population carries the reason ---------------------------------

def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["glab"], returncode, stdout, stderr)


def test_a_glab_failure_names_what_glab_said(monkeypatch):
    monkeypatch.setattr(feed.mrs, "_run",
                        lambda *a, **k: _completed(1, "", "401 Unauthorized"))
    pop, error = feed.fetch_population("@me")
    assert pop is None
    assert "401" in error or "auth" in error.lower()


def test_unparseable_output_is_a_reason_not_an_empty_population(monkeypatch):
    monkeypatch.setattr(feed.mrs, "_run", lambda *a, **k: _completed(0, "not json"))
    pop, error = feed.fetch_population("@me")
    assert pop is None
    assert "JSON" in error


def test_a_missing_glab_tells_the_reader_what_to_install(monkeypatch):
    def _boom(*_a, **_k):
        raise FileNotFoundError("glab")

    monkeypatch.setattr(feed.mrs, "_run", _boom)
    pop, error = feed.fetch_population("@me")
    assert pop is None
    assert "glab" in error


def test_a_scope_with_an_unknown_token_says_which_token(monkeypatch):
    """#939's guard was right and silent. The population is still not
    established — but the reader now learns their scope is the reason."""
    def explode(*_a, **_k):  # pragma: no cover - the guard must not query
        raise AssertionError("the poller must not query GitLab for a scope "
                             "it could not narrow")

    monkeypatch.setattr(feed.mrs, "_run", explode)
    pop, error = feed.fetch_population("mystery=1")
    assert pop is None
    assert "mystery" in error


def test_a_good_scope_still_returns_an_empty_reason(monkeypatch):
    monkeypatch.setattr(feed.mrs, "_run", lambda *a, **k: _completed(0, "[]"))
    assert feed.fetch_population("@me") == ({}, "")


# --- 4. the event is declared, filterable, and on by default ----------------

def test_events_json_lists_exactly_what_the_poller_emits():
    """An event the poller emits but `events.json` omits is unfilterable and
    undiscoverable; one it declares and never emits is an untrue claim."""
    declared = json.loads((SOURCE_DIR / "events.json").read_text(encoding="utf-8"))
    keys = [e["key"] for e in declared["events"]]
    assert len(keys) == len(set(keys))
    assert set(keys) == set(feed.EVENT_KEYS)


def test_no_existing_event_name_moved():
    """`only=` strings live in user config and in radar."""
    declared = json.loads((SOURCE_DIR / "events.json").read_text(encoding="utf-8"))
    keys = {e["key"] for e in declared["events"]}
    assert {"mr_opened", "mr_merged", "mr_closed", "mr_left_feed"} <= keys


def test_mrs_unreachable_is_in_default_feed_only():
    """`DEFAULT_FEED_ONLY` is what every radar run spawns the feed with. An
    outage the operator only learns about by passing a non-default `only=` is
    the defect left exactly where it hurts — the default configuration."""
    assert "mrs_unreachable" in defaults.DEFAULT_FEED_ONLY.split(",")


def test_the_change_is_documented():
    assert_change_is_findable(1602)

# --- 5. the radar board has its own row for a feed that cannot see ----------
#
# Three facts, not two: the poller is not running / it crashed on its last
# tick / it ran fine and could not establish the population. `feed_error`
# answers only the second — the dispatcher writes `last_error` from a poller
# *exception*, and a `(None, why)` fetch is a clean return.

from tiers import gl_mrs  # noqa: E402


@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(gl_mrs.transport, "STATE_DIR", str(tmp_path))
    return tmp_path


def _feed_state(tmp_path: Path, scope: str, **state) -> None:
    name = f"supertool-watch-{gl_mrs.FEED_SOURCE}__{scope}.state.json"
    (tmp_path / name).write_text(json.dumps({"source_state": state}),
                                 encoding="utf-8")


def test_feed_blind_reports_the_reason_the_population_was_not_established(state_dir):
    _feed_state(state_dir, "@me", lookup="unavailable", error=OUTAGE)
    assert gl_mrs.feed_blind("@me") == OUTAGE


def test_feed_blind_is_silent_while_the_feed_can_see(state_dir):
    _feed_state(state_dir, "@me", lookup="ok", known={"1": {}})
    assert gl_mrs.feed_blind("@me") == ""


def test_feed_blind_is_silent_when_there_is_no_state_file_at_all(state_dir):
    """Absence of a state file is not evidence of blindness — a feed spawned
    seconds ago has not written one yet."""
    assert gl_mrs.feed_blind("@me") == ""


def test_the_blind_reason_is_flattened_before_it_reaches_the_board(state_dir):
    """The third render of a `read_state` string (#1197). The poller writes it
    from `glab` stderr into a world-writable file at a predictable name, and
    this board prints it into a multi-line report."""
    _feed_state(state_dir, "@me", lookup="unavailable",
                error="boom\ngitlab-mr       19509  32471  2026-08-07  all green")
    message = gl_mrs.feed_blind("@me")
    assert "\n" not in message, message
    assert "all green" in message, "the forgery must be disclosed, not dropped"


def test_a_blind_feed_gets_its_own_warning_line(state_dir):
    lines = gl_mrs.render([], set(), [], {}, [], [], {"mrs": {}},
                          feed="alive", feed_blind=OUTAGE, label="scope x")
    warning = [ln for ln in lines if ln.startswith("radar: WARNING")]
    assert len(warning) == 1, lines
    assert OUTAGE in warning[0]
    # Distinct from the crashed-poller line: "failing to poll" is a poller
    # that raised, and this one did not.
    assert "failing to poll" not in warning[0]


def test_a_seeing_feed_prints_no_warning(state_dir):
    """The absence of the line is the positive claim."""
    lines = gl_mrs.render([], set(), [], {}, [], [], {"mrs": {}},
                          feed="alive", feed_blind="", label="scope x")
    assert not [ln for ln in lines if ln.startswith("radar: WARNING")], lines


def _stub_tier(monkeypatch, blind: str):
    monkeypatch.setattr(gl_mrs, "live_open_mrs", lambda multi: [])
    monkeypatch.setattr(gl_mrs, "read_state_files", lambda: {})
    monkeypatch.setattr(gl_mrs, "prune_terminal", lambda states, watched: [])
    monkeypatch.setattr(gl_mrs, "drift", lambda states: {})
    monkeypatch.setattr(gl_mrs, "heal", lambda ids, watched: ([], [], []))
    monkeypatch.setattr(gl_mrs, "feed_scope", lambda multi: "@me")
    monkeypatch.setattr(gl_mrs, "feed_error", lambda scope: "")
    monkeypatch.setattr(gl_mrs, "feed_blind", lambda scope: blind)
    monkeypatch.setattr(gl_mrs, "other_feed_scopes", lambda scope: [])
    monkeypatch.setattr(gl_mrs, "read_exclusions", lambda: ({}, []))
    monkeypatch.setattr(gl_mrs, "read_snapshot", lambda multi: {"mrs": {}})
    monkeypatch.setattr(gl_mrs, "write_snapshot", lambda entries, multi: None)
    monkeypatch.setattr(gl_mrs.mrs, "_watched_iids", lambda *a, **k: set())
    monkeypatch.setattr(gl_mrs.mrs, "_get_config", lambda: {"per_page": 100})
    return {"_watch": lambda *a, **k: "alive"}


def test_a_blind_board_is_not_healthy(monkeypatch):
    """`healthy` means "this tier could tell you the truth", and radar's
    `quiet_when_healthy` drops a healthy tier's lines wholesale — so claiming
    health here would delete the warning line above on the way out."""
    options = _stub_tier(monkeypatch, OUTAGE)
    lines, healthy = gl_mrs.radar_report(options)
    assert healthy is False
    assert any(OUTAGE in ln for ln in lines), lines


def test_a_seeing_board_with_nothing_to_report_stays_healthy(monkeypatch):
    """The control. Without it the test above passes against a tier that
    reports every board as broken."""
    options = _stub_tier(monkeypatch, "")
    _lines, healthy = gl_mrs.radar_report(options)
    assert healthy is True


def test_radar_state_names_the_blindness_without_calling_gitlab(state_dir, monkeypatch):
    """`radar:--state` is the view that spawns nothing. A feed that cannot see
    is exactly what someone opening it is looking for."""
    monkeypatch.setattr(gl_mrs, "feed_scope", lambda multi: "@me")
    monkeypatch.setattr(gl_mrs, "feed_blind", lambda scope: OUTAGE)
    monkeypatch.setattr(gl_mrs, "read_snapshot", lambda multi: {"mrs": {}})
    monkeypatch.setattr(gl_mrs.mrs, "_watched_iids", lambda *a, **k: set())
    monkeypatch.setattr(gl_mrs, "read_exclusions", lambda: ({}, []))
    lines = gl_mrs.radar_state({})
    assert any(OUTAGE in ln for ln in lines), lines

def test_the_board_and_the_poller_agree_on_the_flag_string():
    """`gl_mrs` re-spells `LOOKUP_UNAVAILABLE` rather than importing it, to
    keep a board render from loading the source module. A copy that drifts is
    a check that never matches, which reads exactly like a feed that can see."""
    from tiers import gl_mrs as tier  # noqa: PLC0415

    assert tier.FEED_LOOKUP_UNAVAILABLE == feed.LOOKUP_UNAVAILABLE
