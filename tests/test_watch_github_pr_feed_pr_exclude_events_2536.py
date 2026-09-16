"""claude-supertool#2536: the feed's own per-PR forks ignore pr_exclude_events.

`heal()` (radar's own per-PR healer) already forks a `github-pr` poller with
`poller_only(excluded)` -- the `pr_exclude_events` blacklist resolved once by
the `gh-prs` tier (claude-oss#1499). `github-pr-feed`'s `spawn_watcher` is the
*other* forker of the same poller, called from inside the feed's own process
when it discovers a PR mid-poll, and it used to hardcode `only=[]` -- no
filter, unconditionally, regardless of what the tier's blacklist said.

Since the loop opens nearly every PR between two `radar` calls, nearly every
per-PR poller in practice is feed-forked, so the blacklist filtered almost
nothing (claude-supertool#2536's "Why it matters"). The fix hands the feed the
same list, resolved by the same function (`gh_prs.poller_only`), through the
one channel already available: `SUPERTOOL_RADAR_TIERS`, present in the feed's
own environment exactly when radar forked it (`poller_env()` copies the whole
environment into every poller it execs). A feed started by hand has no such
variable and keeps today's unfiltered `[]`.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).parent.parent
SOURCE_DIR = REPO / "presets" / "watch" / "sources" / "github-pr-feed"
WATCH_DIR = REPO / "presets" / "watch"

_spec = importlib.util.spec_from_file_location("gh_pr_feed_poller_2536", SOURCE_DIR / "poller.py")
assert _spec is not None and _spec.loader is not None
feed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(feed)

TIERS_ENV = "SUPERTOOL_RADAR_TIERS"

with open(WATCH_DIR / "sources" / "github-pr" / "events.json", encoding="utf-8") as _f:
    ALL_EVENTS = [e["key"] for e in json.load(_f)["events"]]


@pytest.fixture(autouse=True)
def reset_cache(monkeypatch):
    """`pr_only()` caches for the life of a real process; a fresh test is a
    fresh process as far as this cache is concerned, so it must not leak
    between tests -- otherwise the second test to run would silently see the
    first test's answer."""
    monkeypatch.setattr(feed, "_pr_only_cache", None)
    monkeypatch.delenv(TIERS_ENV, raising=False)


class _FakeDispatcher(ModuleType):
    def __init__(self):
        super().__init__("fake_dispatcher_2536")
        self.calls: list[tuple[str, str, list[str]]] = []
        self.spawn_ok = True

    def _spawn_poller(self, source, watcher_id, only):
        self.calls.append((source, watcher_id, list(only)))
        return 4242 if self.spawn_ok else 0


@pytest.fixture
def fake_dispatcher(monkeypatch):
    fake = _FakeDispatcher()
    monkeypatch.setattr(feed, "_dispatcher", lambda: fake)
    return fake


def _set_exclude(monkeypatch, excluded: list[str]) -> None:
    monkeypatch.setenv(TIERS_ENV, json.dumps({"gh-prs": {"pr_exclude_events": excluded}}))


# ---------------------------------------------------------------------------
# spawn_watcher applies the resolved list
# ---------------------------------------------------------------------------

def test_spawn_watcher_excludes_the_blacklisted_keys(monkeypatch, fake_dispatcher):
    excluded = ["checks_pending", "checks_succeeded", "conflicts_appeared"]
    _set_exclude(monkeypatch, excluded)

    assert feed.spawn_watcher("1509") is True

    assert fake_dispatcher.calls == [("github-pr", "1509", fake_dispatcher.calls[0][2])]
    source, watcher_id, only = fake_dispatcher.calls[0]
    # Negative: none of the blacklisted keys reach the per-PR poller's argv.
    for key in excluded:
        assert key not in only
    # Positive control: every other declared key still does -- an empty
    # `only` would pass the negative half alone and mean "no filter", the
    # opposite of what was configured, and the #2536 bug this test pins.
    remaining = [k for k in ALL_EVENTS if k not in excluded]
    assert sorted(only) == sorted(remaining)


def test_no_radar_tiers_env_keeps_todays_unfiltered_spawn(monkeypatch, fake_dispatcher):
    # No SUPERTOOL_RADAR_TIERS at all -- a feed started by hand, per the
    # issue's own "keeps today's behaviour" clause.
    assert feed.spawn_watcher("77") is True
    _source, _id, only = fake_dispatcher.calls[0]
    assert only == []


def test_gh_prs_tier_absent_from_radar_tiers_keeps_unfiltered_spawn(monkeypatch, fake_dispatcher):
    # SUPERTOOL_RADAR_TIERS is set (some other tier is configured) but names
    # no gh-prs entry at all.
    monkeypatch.setenv(TIERS_ENV, json.dumps({"gl-mrs": {}}))
    feed.spawn_watcher("77")
    _source, _id, only = fake_dispatcher.calls[0]
    assert only == []


def test_malformed_pr_exclude_events_falls_back_to_unfiltered_rather_than_raising(
        monkeypatch, fake_dispatcher):
    # radar itself refuses a bad pr_exclude_events loudly before spawning
    # anything; this source's job is discovery, not re-validating a config
    # radar already validated -- so it must not take the whole feed process
    # down over one.
    monkeypatch.setenv(TIERS_ENV, json.dumps({"gh-prs": {"pr_exclude_events": ["not-a-real-key"]}}))
    assert feed.spawn_watcher("77") is True
    _source, _id, only = fake_dispatcher.calls[0]
    assert only == []


# ---------------------------------------------------------------------------
# terminal_coverage's spawned-here shortcut must match what was actually forked
# ---------------------------------------------------------------------------

def test_terminal_coverage_of_a_freshly_spawned_poller_matches_pr_only(monkeypatch):
    excluded = ["merged"]
    _set_exclude(monkeypatch, excluded)
    covers = feed.terminal_coverage("1509", set(), spawned=True)
    # "merged" was excluded, so a poller spawned with this filter does not
    # cover it -- covering it here (the pre-fix hardcoded []) would be a
    # feed that believes a filtered-out poller reports an event it cannot.
    assert "merged" not in covers
    assert "closed" in covers


# ---------------------------------------------------------------------------
# pr_only() itself
# ---------------------------------------------------------------------------

def test_pr_only_is_cached_for_the_life_of_the_process(monkeypatch):
    _set_exclude(monkeypatch, ["checks_pending"])
    first = feed.pr_only()
    assert "checks_pending" not in first
    # The environment of a running process cannot change under it either way
    # -- documented here so a later change to make this "live" is a decision,
    # not an accident.
    monkeypatch.setenv(TIERS_ENV, json.dumps({"gh-prs": {"pr_exclude_events": []}}))
    assert feed.pr_only() == first
