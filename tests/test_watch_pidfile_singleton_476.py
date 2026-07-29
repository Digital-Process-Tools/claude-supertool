"""#476 — a watch poller slot must be claimed atomically, before the spawn.

Observed: nine `radar.py` processes, several in same-second groups with
byte-identical arguments, plus a day-old `dispatcher.py`. All PPID 1.

The argv is the tell. `_spawn_poller` double-forks, so a poller *inherits the
argv of whatever process forked it* — a "radar.py author=@me,..." process aged
two hours is not a radar run, it is a feed poller radar spawned. So the nine
processes are duplicate pollers, and the same-second grouping names the defect:
a race, not a missing check.

Both spawn sites already had a check. Neither was atomic:

  - `cmd_watch`      : `os.path.exists(pid_file)` and `_pid_alive(...)`
  - the feed watcher (`radar.ensure_watcher`): `feed_pid()` and `_pid_alive(...)`

and the pidfile they test is written by the *grandchild*, after a fork, an
import and a detach. Every caller that looks inside that window sees an empty
slot and spawns its own. This is #451 in a second place, and it takes #451's
answer: claim the slot with `O_CREAT|O_EXCL` **before** any side effect, so
losing the race is free — notice, say so, touch nothing.

Identity key: `(source, id)`, which is what `transport.pid_path` already keys
on and what both tiers already pass. It is deliberately not tightened into
anything cleverer. The two false results are not symmetric — a key that is too
loose leaks a duplicate poller (visible in `ps`, visible in `watches`, and the
thing this issue is about), while a key that is too tight silently refuses to
start a watcher someone asked for, which renders as "nothing to report". The
second failure is the one this repo keeps filing issues about, so the tie goes
to leaking. The one tightening here is free of that risk: the *feed* id is a
filter string, and set-identical filters that differ only in ordering describe
one population, so canonicalising the order can never merge two filters that
would have selected differently.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

WATCH_DIR = Path(__file__).parent.parent / "presets" / "watch"
sys.path.insert(0, str(WATCH_DIR))

import transport  # noqa: E402  (the same module object dispatcher/radar import)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dispatcher = _load("watch_dispatcher_476", WATCH_DIR / "dispatcher.py")
radar = _load("watch_radar_476", WATCH_DIR / "radar.py")
mr_tier = _load("watch_radar_476_gl_mrs", WATCH_DIR / "tiers" / "gl_mrs.py")


def _dead_pid() -> int:
    """A PID that certainly belongs to no live process."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    """Every pid/state path under the test's own dir, never the real /tmp."""
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(dispatcher.transport, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(radar.transport, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(radar.dispatcher.transport, "STATE_DIR", str(tmp_path))
    return tmp_path


class _Spawns:
    """A `_spawn_poller` stand-in that writes no pidfile — like the real one.

    The real grandchild publishes its PID only after fork + interpreter start +
    detach. A fake that writes the pidfile synchronously would paper over
    exactly the window this issue is about, so this one does not write it.
    """

    def __init__(self, pid: int | None = None):
        self.calls: list[tuple[str, str, list[str]]] = []
        self.pid = os.getpid() if pid is None else pid

    def __call__(self, source: str, watcher_id: str, only: list[str]) -> int:
        self.calls.append((source, watcher_id, list(only)))
        return self.pid


# ---------------------------------------------------------------------------
# the claim itself
# ---------------------------------------------------------------------------

def test_claim_pidfile_admits_exactly_one_claimant() -> None:
    assert transport.claim_pidfile("gitlab-mr", "1") == 0
    assert transport.claim_pidfile("gitlab-mr", "1") == os.getpid()


def test_claim_pidfile_reclaims_a_slot_whose_owner_is_dead() -> None:
    """A crashed poller must not wedge its slot shut forever.

    That failure is worse than a duplicate: it leaves the population with no
    watcher at all, and an absent watcher reads as "nothing to report".
    """
    transport.record_pid("gitlab-mr", "2", _dead_pid())
    assert transport.claim_pidfile("gitlab-mr", "2") == 0
    assert transport.read_pid("gitlab-mr", "2") == os.getpid()


def test_claim_pidfile_reclaims_an_unreadable_pidfile(state_dir) -> None:
    Path(transport.pid_path("gitlab-mr", "3")).write_text("not a pid\n", encoding="utf-8")
    assert transport.claim_pidfile("gitlab-mr", "3") == 0


def test_release_pidfile_leaves_another_owners_claim_alone() -> None:
    """An exiting poller whose slot was already reclaimed must not clear it."""
    assert transport.claim_pidfile("gitlab-mr", "4") == 0
    transport.record_pid("gitlab-mr", "4", 424242)
    transport.release_pidfile("gitlab-mr", "4", os.getpid())
    assert transport.read_pid("gitlab-mr", "4") == 424242


# ---------------------------------------------------------------------------
# cmd_watch — the per-MR tier
# ---------------------------------------------------------------------------

def test_second_watch_during_the_startup_window_does_not_spawn(monkeypatch) -> None:
    """The race, pinned. Two starts, no pidfile published yet — one process."""
    spawns = _Spawns()
    monkeypatch.setattr(dispatcher, "_spawn_poller", spawns)
    assert dispatcher.cmd_watch(["gitlab-mr", "21803"]) == 0
    assert dispatcher.cmd_watch(["gitlab-mr", "21803"]) == 0
    assert len(spawns.calls) == 1


def test_refused_watch_names_the_live_pid_holding_the_slot(monkeypatch, capsys) -> None:
    """A refusal that renders like a success is the failure mode to avoid."""
    spawns = _Spawns()
    monkeypatch.setattr(dispatcher, "_spawn_poller", spawns)
    dispatcher.cmd_watch(["gitlab-mr", "21803"])
    capsys.readouterr()
    dispatcher.cmd_watch(["gitlab-mr", "21803"])
    out = capsys.readouterr().out
    assert "Already watching" in out
    assert str(os.getpid()) in out
    assert "unwatch:gitlab-mr:21803" in out


def test_two_genuinely_different_watches_both_start(monkeypatch) -> None:
    spawns = _Spawns()
    monkeypatch.setattr(dispatcher, "_spawn_poller", spawns)
    dispatcher.cmd_watch(["gitlab-mr", "21803"])
    dispatcher.cmd_watch(["gitlab-mr", "21804"])
    dispatcher.cmd_watch(["github-pr", "21803"])
    assert len(spawns.calls) == 3


def test_watch_reclaims_the_slot_of_a_dead_poller(monkeypatch) -> None:
    transport.record_pid("gitlab-mr", "21803", _dead_pid())
    spawns = _Spawns()
    monkeypatch.setattr(dispatcher, "_spawn_poller", spawns)
    assert dispatcher.cmd_watch(["gitlab-mr", "21803"]) == 0
    assert len(spawns.calls) == 1


def test_a_failed_spawn_releases_the_slot(monkeypatch, capsys) -> None:
    """A claim held by a spawn that never happened is a permanently blind slot."""
    monkeypatch.setattr(dispatcher, "_spawn_poller", _Spawns(pid=0))
    dispatcher.cmd_watch(["gitlab-mr", "21803"])
    assert not os.path.exists(transport.pid_path("gitlab-mr", "21803"))
    assert "ERROR" in capsys.readouterr().out

    spawns = _Spawns()
    monkeypatch.setattr(dispatcher, "_spawn_poller", spawns)
    dispatcher.cmd_watch(["gitlab-mr", "21803"])
    assert len(spawns.calls) == 1


def test_the_claim_precedes_the_spawn(monkeypatch) -> None:
    """#451's ordering, held: the pidfile exists before the child is forked."""
    seen: dict[str, int] = {}

    def _spawn(source: str, watcher_id: str, only: list[str]) -> int:
        seen["claimed"] = transport.read_pid(source, watcher_id)
        return os.getpid()

    monkeypatch.setattr(dispatcher, "_spawn_poller", _spawn)
    dispatcher.cmd_watch(["gitlab-mr", "21803"])
    assert seen["claimed"] == os.getpid()


# ---------------------------------------------------------------------------
# the feed watcher — the discovery guarantee, and the source of the observed nine
# ---------------------------------------------------------------------------

def test_the_feed_watcher_does_not_stack_a_second_feed(monkeypatch) -> None:
    spawns = _Spawns()
    monkeypatch.setattr(radar.dispatcher, "_spawn_poller", spawns)
    assert radar.ensure_watcher(mr_tier.FEED_SOURCE, "@me", mr_tier.feed_only()) == "spawned"
    assert radar.ensure_watcher(mr_tier.FEED_SOURCE, "@me", mr_tier.feed_only()) == "alive"
    assert len(spawns.calls) == 1


def test_the_feed_watcher_respawns_over_a_dead_feed(monkeypatch) -> None:
    transport.record_pid(mr_tier.FEED_SOURCE, "@me", _dead_pid())
    spawns = _Spawns()
    monkeypatch.setattr(radar.dispatcher, "_spawn_poller", spawns)
    assert radar.ensure_watcher(mr_tier.FEED_SOURCE, "@me", mr_tier.feed_only()) == "spawned"
    assert len(spawns.calls) == 1


def test_the_feed_watcher_releases_the_slot_when_the_spawn_fails(monkeypatch) -> None:
    monkeypatch.setattr(radar.dispatcher, "_spawn_poller", _Spawns(pid=0))
    assert radar.ensure_watcher(mr_tier.FEED_SOURCE, "@me", mr_tier.feed_only()) == "failed"
    assert not os.path.exists(transport.pid_path(mr_tier.FEED_SOURCE, "@me"))


# ---------------------------------------------------------------------------
# mr_tier.heal — the per-MR tier (#417 item 1 made this the tier with the
# multiplier: the population went from "the few that are red" to "every open
# MR", so a race here duplicates ~7 pollers, not one)
# ---------------------------------------------------------------------------

def test_heal_does_not_stack_a_second_watcher(monkeypatch) -> None:
    """The third spawn tier must come through the same door as the other two.

    `heal` derives its gaps from a `watched` set computed by its caller, which
    is the same test-then-fork shape #476 was filed about — and the pidfile it
    is derived from is published by the grandchild, after a fork and a detach.
    Two radars in that window both see the gap and both spawn.
    """
    spawns = _Spawns()
    monkeypatch.setattr(radar.dispatcher, "_spawn_poller", spawns)
    assert mr_tier.heal(["33161"], set())[:2] == (["33161"], [])
    mr_tier.heal(["33161"], set())
    assert len(spawns.calls) == 1


def test_heal_claims_the_slot_before_forking(monkeypatch) -> None:
    """#451's ordering, in the tier that never got it."""
    seen: dict[str, int] = {}

    def _spawn(source: str, watcher_id: str, only: list[str]) -> int:
        seen["claimed"] = transport.read_pid(source, watcher_id)
        return os.getpid()

    monkeypatch.setattr(radar.dispatcher, "_spawn_poller", _spawn)
    mr_tier.heal(["33161"], set())
    assert seen["claimed"] == os.getpid()


def test_an_mr_watched_by_another_radar_is_neither_healed_nor_uncovered(
    monkeypatch,
) -> None:
    """Losing the race is not a failure, and it is not an achievement either.

    Reported healed, radar claims an action it did not take. Reported
    uncovered, radar warns that an MR with a live watcher is unwatched — and
    that warning is the one thing on this board a reader must be able to
    trust, because it is the whole point of the op.
    """
    monkeypatch.setattr(radar.dispatcher, "_spawn_poller", _Spawns())
    mr_tier.heal(["33161"], set())
    healed, uncovered, refused = mr_tier.heal(["33161"], set())
    assert healed == []
    assert uncovered == []
    assert refused == []


def test_heal_releases_the_slot_when_the_spawn_fails(monkeypatch) -> None:
    """A claim left behind by a poller that never started refuses every future
    heal for that iid — and an MR quietly dropped from the fleet is the exact
    failure #417 exists to remove."""
    monkeypatch.setattr(radar.dispatcher, "_spawn_poller", _Spawns(pid=0))
    assert mr_tier.heal(["33161"], set())[:2] == ([], ["33161"])
    assert not os.path.exists(transport.pid_path(mr_tier.SOURCE, "33161"))

    spawns = _Spawns()
    monkeypatch.setattr(radar.dispatcher, "_spawn_poller", spawns)
    assert mr_tier.heal(["33161"], set())[:2] == (["33161"], [])


def test_feed_scope_is_insensitive_to_filter_order() -> None:
    """The feed id *is* the pid filename, so two spellings are two pollers.

    Safe to canonicalise because it only merges filters that are already the
    same set — it can never refuse a filter that selects something different.
    """
    one = mr_tier.mrs._parse_multi("author=@me,author=modular.system,state=opened")[0]
    other = mr_tier.mrs._parse_multi("state=opened,author=modular.system,author=@me")[0]
    assert mr_tier.feed_scope(one) == mr_tier.feed_scope(other)
