"""Tests for radar's reap (issue #749 item 3).

`radar` respawned a fleet without ever stopping one. Pollers survived session
restarts, accumulated across days, and every survivor on a slot emitted every
event independently: 36 live processes against 18 tracked, one MR announcement
arriving 13 times.

The reap is bounded by what a PID can *prove* about itself. A labelled poller
names its own slot in its own argv (`transport.poller_argv`, #511), so two
pollers on one slot are provably duplicates of each other and stopping all but
one provably leaves the slot covered. Nothing else here is killed: not a lone
poller, not an orphan that is the only thing covering its slot, and nothing at
all when the scan that would have found the duplicates could not run — that
last one declines out loud rather than rendering a clean board.

No test in this file may signal a real process. `_stop_pid` is patched
everywhere, the fleet is a dict, and liveness is a set.
"""
from __future__ import annotations

import importlib.util
import os
import types
from pathlib import Path

import pytest

WATCH_DIR = Path(__file__).parent.parent / "presets" / "watch"


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


radar = _module("watch_radar_reap", WATCH_DIR / "radar.py")
dispatcher = radar.dispatcher
transport = radar.transport


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """A fake poller fleet: a scan, a liveness set, and a kill recorder.

    Nothing here touches a real process. `killed` records the argument of every
    `_stop_pid` call — one PID per call, which is also the assertion in
    `test_reap_stops_one_pid_per_call`.
    """
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path))
    state = {
        "scan": {},          # {(source, id): [pid, ...]}
        "scan_ok": True,
        "live": set(),       # pids that answer as alive
        "killed": [],        # [pid, ...] in call order
        "refuse": {},        # {pid: "why it could not be stopped"}
    }

    monkeypatch.setattr(transport, "scan_poller_pids",
                        lambda: ({k: sorted(v) for k, v in state["scan"].items()},
                                 state["scan_ok"]))
    monkeypatch.setattr(transport, "_pid_alive", lambda pid: pid in state["live"])

    def _fake_stop(pid: int) -> str:
        state["killed"].append(pid)
        why = state["refuse"].get(pid, "")
        if not why:
            state["live"].discard(pid)
        return why

    monkeypatch.setattr(dispatcher, "_stop_pid", _fake_stop)
    # Default: a platform that *has* `ps`, so a failed scan is a failure rather
    # than a capability that was never there. The platform case is patched
    # explicitly by the tests that are about it.
    state["real_ps_scan_supported"] = transport.ps_scan_supported
    monkeypatch.setattr(transport, "ps_scan_supported", lambda: state["ps"])
    state["ps"] = True
    state["dir"] = tmp_path
    state["monkeypatch"] = monkeypatch
    return state


def _slot(fleet, source: str, watcher_id: str, pids: list[int], *, tracked: int | None = None):
    """Put `pids` on a slot, all alive, and optionally write its pid file."""
    fleet["scan"][(source, watcher_id)] = list(pids)
    fleet["live"].update(pids)
    if tracked is not None:
        path = fleet["dir"] / f"supertool-watch-{source}__{watcher_id}.pid"
        path.write_text(f"{tracked}\n")


# ---------------------------------------------------------------------------
# what gets killed
# ---------------------------------------------------------------------------

def test_duplicate_pollers_on_one_slot_are_reaped_down_to_one(fleet):
    _slot(fleet, "gitlab-mr", "33311", [101, 102, 103], tracked=101)

    dispatcher.reap_duplicate_pollers()

    assert sorted(fleet["killed"]) == [102, 103]
    assert 101 in fleet["live"]


def test_the_pidfile_owner_is_the_survivor(fleet):
    """Not the lowest PID — the one `watches` and `unwatch` already name."""
    _slot(fleet, "gitlab-mr", "33311", [101, 102, 103], tracked=103)

    dispatcher.reap_duplicate_pollers()

    assert sorted(fleet["killed"]) == [101, 102]
    assert 103 in fleet["live"]


def test_a_slot_with_one_poller_is_left_alone(fleet):
    _slot(fleet, "gitlab-mr", "33311", [101], tracked=101)

    lines = dispatcher.reap_duplicate_pollers()

    assert fleet["killed"] == []
    assert lines == []


def test_a_lone_orphan_is_not_killed(fleet):
    """No pid file, but it is the only thing polling that slot.

    Killing it would trade a duplicate nobody has for a slot nobody watches,
    which is the trade #513 says is the wrong way round.
    """
    _slot(fleet, "gitlab-mr", "33311", [101])

    dispatcher.reap_duplicate_pollers()

    assert fleet["killed"] == []


def test_an_orphan_slot_keeps_a_deterministic_survivor(fleet):
    """Duplicates with no pid file to arbitrate: keep the lowest PID, once."""
    _slot(fleet, "gitlab-mr-feed", "author=@me", [305, 101, 202])

    dispatcher.reap_duplicate_pollers()

    assert sorted(fleet["killed"]) == [202, 305]
    assert 101 in fleet["live"]


def test_a_dead_tracked_pid_does_not_make_a_lone_poller_a_duplicate(fleet):
    """The pid file names a PID that is gone; one live poller remains."""
    _slot(fleet, "gitlab-mr", "33311", [101], tracked=999999)

    dispatcher.reap_duplicate_pollers()

    assert fleet["killed"] == []


def test_only_live_pids_are_signalled(fleet):
    fleet["scan"][("gitlab-mr", "33311")] = [101, 102, 103]
    fleet["live"].update([101, 102])

    dispatcher.reap_duplicate_pollers()

    assert fleet["killed"] == [102]


def test_slots_are_reaped_independently(fleet):
    _slot(fleet, "gitlab-mr", "33311", [101, 102], tracked=101)
    _slot(fleet, "gitlab-mr", "33312", [201], tracked=201)

    dispatcher.reap_duplicate_pollers()

    assert fleet["killed"] == [102]


def test_reap_stops_one_pid_per_call(fleet):
    """A batched `kill $PID_LIST` against these processes silently no-ops.

    Exit 0, every process still alive, `-9` included. A per-PID loop stops them.
    Nothing here may grow a batched form: it would look exactly like a working
    reap and leave the whole fleet emitting.
    """
    _slot(fleet, "gitlab-mr", "33311", [101, 102, 103, 104], tracked=101)

    dispatcher.reap_duplicate_pollers()

    assert fleet["killed"] == [102, 103, 104]
    assert all(isinstance(pid, int) for pid in fleet["killed"])


# ---------------------------------------------------------------------------
# what gets said
# ---------------------------------------------------------------------------

def test_every_reaped_pid_is_named(fleet):
    _slot(fleet, "gitlab-mr", "33311", [101, 102, 103], tracked=101)

    lines = dispatcher.reap_duplicate_pollers()

    assert len(lines) == 1
    assert "gitlab-mr:33311" in lines[0]
    assert "102" in lines[0] and "103" in lines[0]
    assert "101" in lines[0]


def test_a_kill_that_failed_is_named_not_swallowed(fleet):
    _slot(fleet, "gitlab-mr", "33311", [101, 102], tracked=101)
    fleet["refuse"][102] = "Operation not permitted"

    lines = dispatcher.reap_duplicate_pollers()

    joined = "\n".join(lines)
    assert "102" in joined
    assert "Operation not permitted" in joined
    assert "WARNING" in joined


def test_reap_declines_when_the_process_scan_is_unavailable(fleet):
    """The third state: not ok, not a finding — no information.

    A reaper that cannot see the fleet and prints nothing renders exactly like
    one that looked and found it clean.
    """
    _slot(fleet, "gitlab-mr", "33311", [101, 102], tracked=101)
    fleet["scan_ok"] = False

    lines = dispatcher.reap_duplicate_pollers()

    assert fleet["killed"] == []
    assert len(lines) == 1
    assert "skipped" in lines[0]


def test_a_platform_that_cannot_scan_at_all_does_not_say_so_every_run(fleet):
    """Windows has no `ps`, so `scan_ok` is False on every run, forever.

    A line that prints unconditionally on a whole platform is not disclosure,
    it is furniture: readers learn to skim it, and then it cannot do its job on
    the machine where the scan *could* have worked and genuinely did not. The
    absence is stated where a user asks about the fleet on purpose — `watches`,
    tested below — not on every radar board.
    """
    _slot(fleet, "gitlab-mr", "33311", [101, 102], tracked=101)
    fleet["scan_ok"] = False
    fleet["ps"] = False

    lines = dispatcher.reap_duplicate_pollers()

    assert lines == []
    assert fleet["killed"] == []


def test_a_scan_that_failed_where_ps_exists_still_declines_loudly(fleet):
    """The distinction is capability, not message-matching: `ps` is here and
    did not answer, which is news every single time."""
    _slot(fleet, "gitlab-mr", "33311", [101, 102], tracked=101)
    fleet["scan_ok"] = False
    fleet["ps"] = True

    lines = dispatcher.reap_duplicate_pollers()

    assert len(lines) == 1 and "skipped" in lines[0]
    assert fleet["killed"] == []


def test_ps_support_is_decided_by_looking_for_the_binary(fleet):
    """Not by platform name, and not by parsing a failure message."""
    real = fleet["real_ps_scan_supported"]

    fleet["monkeypatch"].setattr(transport.shutil, "which", lambda name: None)
    assert real() is False

    fleet["monkeypatch"].setattr(transport.shutil, "which", lambda name: "/bin/ps")
    assert real() is True


def test_watches_says_the_platform_can_never_scan(fleet, capsys):
    """The deliberate surface carries the permanent absence.

    Someone running `watches` is asking about the fleet; that is where "this
    machine can never see an untracked poller" belongs, and it is where a
    Windows user already gets told the scan did not run.
    """
    fleet["ps"] = False
    fleet["monkeypatch"].setattr(
        transport, "list_watchers",
        lambda: ([{"source": "gitlab-mr", "id": "33311", "pid": 101, "pids": [101],
                   "extra": [], "orphan": False, "dead": False, "deaths": [],
                   "started": "", "last_event": ""}], False))

    dispatcher.cmd_list()
    out = capsys.readouterr().out

    assert "`ps`" in out
    assert "radar" in out


def test_watches_says_a_present_ps_failed_this_time(fleet, capsys):
    fleet["ps"] = True
    fleet["monkeypatch"].setattr(
        transport, "list_watchers",
        lambda: ([{"source": "gitlab-mr", "id": "33311", "pid": 101, "pids": [101],
                   "extra": [], "orphan": False, "dead": False, "deaths": [],
                   "started": "", "last_event": ""}], False))

    dispatcher.cmd_list()
    out = capsys.readouterr().out

    assert "this time" in out
    assert "no `ps` on this platform" not in out
    assert "radar cannot" not in out


def test_a_clean_fleet_is_silent(fleet):
    _slot(fleet, "gitlab-mr", "33311", [101], tracked=101)
    _slot(fleet, "gitlab-mr", "33312", [201], tracked=201)

    assert dispatcher.reap_duplicate_pollers() == []


# ---------------------------------------------------------------------------
# radar wiring
# ---------------------------------------------------------------------------

@pytest.fixture
def radar_env(fleet):
    """A one-tier radar whose tier asks for a watcher, with spawning faked."""
    monkeypatch = fleet["monkeypatch"]
    order: list[str] = []

    real_reap = dispatcher.reap_duplicate_pollers

    def _reap():
        order.append("reap")
        return real_reap()

    def _start(source, watcher_id, only):
        order.append(f"spawn:{source}:{watcher_id}")
        return "spawned", 4242

    monkeypatch.setattr(dispatcher, "reap_duplicate_pollers", _reap)
    monkeypatch.setattr(dispatcher, "start_poller", _start)
    monkeypatch.setattr(dispatcher, "_load_source", lambda name: object())
    monkeypatch.setattr(transport, "deaths", lambda *a: [])

    tier = types.SimpleNamespace(
        RADAR_OPTIONS={"quiet_when_healthy"},
        RADAR_QUIET_DEFAULT=True,
        radar_report=lambda opts: (opts["_watch"]("gitlab-mr", "33311") and [], True),
    )
    monkeypatch.setenv(radar.TIERS_ENV, '{"fake": {}}')
    monkeypatch.setattr(radar, "_tier_module", lambda n: tier if n == "fake" else None)
    fleet["order"] = order
    return fleet


def test_radar_reaps_before_it_spawns(radar_env, capsys):
    """Reaping after the spawn would kill this run's own new pollers, and
    reaping never would leave every restart adding another emitter."""
    radar.main([])

    assert radar_env["order"] == ["reap", "spawn:gitlab-mr:33311"]


def test_radar_prints_what_the_reap_stopped(radar_env, capsys):
    _slot(radar_env, "gitlab-mr", "33311", [101, 102], tracked=101)

    radar.main([])
    out = capsys.readouterr().out

    assert "102" in out
    assert "gitlab-mr:33311" in out


def test_radar_prints_the_reap_decline(radar_env, capsys):
    _slot(radar_env, "gitlab-mr", "33311", [101, 102], tracked=101)
    radar_env["scan_ok"] = False

    radar.main([])
    out = capsys.readouterr().out

    assert "skipped" in out
    assert radar_env["killed"] == []


def test_radar_is_silent_where_the_platform_cannot_scan(radar_env, capsys):
    """The four pre-existing exact-output tests in test_watch_radar.py are the
    ones that caught this: on Windows they saw the decline on every board."""
    _slot(radar_env, "gitlab-mr", "33311", [101, 102], tracked=101)
    radar_env["scan_ok"] = False
    radar_env["ps"] = False

    radar.main([])

    assert capsys.readouterr().out == ""
    assert radar_env["killed"] == []


def test_radar_says_nothing_extra_on_a_clean_fleet(radar_env, capsys):
    _slot(radar_env, "gitlab-mr", "33311", [101], tracked=101)

    radar.main([])

    assert capsys.readouterr().out == ""


def test_no_real_process_is_ever_signalled(fleet):
    """This file's own guard: `os.kill` is not reachable from these tests."""
    _slot(fleet, "gitlab-mr", "33311", [os.getpid(), os.getpid() + 1], tracked=os.getpid())

    dispatcher.reap_duplicate_pollers()

    assert os.getpid() not in fleet["killed"]
