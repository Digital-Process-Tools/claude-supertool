"""Reaping belongs to the runs that spawn, not to every run (issue #957).

`main()` reaped unconditionally, above every tier. So a radar invocation that
established no coverage at all — a tier that raised before it could spawn, a
tier with no watchers to keep alive — still stopped processes on its way past.
That is action charged to a call that only looked, and this repository's
standing rule is that having side effects and being inspectable are two
different properties.

The bound this file pins: radar reaps when, and only when, a tier asks it to
spawn — still *before* that first spawn, because a reap that ran afterwards
would be judging this run's own new pollers (#749). A run that spawns nothing
has no duplicate of its own making to remove, so it removes nothing.

No test here may signal a real process: `_stop_pid` is patched, the fleet is a
dict, liveness is a set.
"""
from __future__ import annotations

import importlib.util
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


radar = _module("watch_radar_957", WATCH_DIR / "radar.py")
dispatcher = radar.dispatcher
transport = radar.transport


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """A fake poller fleet with one slot carrying a duplicate.

    The duplicate is the whole point: every test here asserts about what did or
    did not happen to PID 102, which a reap that runs *would* stop.
    """
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path))
    state: dict = {"scan": {}, "scan_ok": True, "live": set(), "killed": [], "ps": True}

    monkeypatch.setattr(transport, "scan_poller_pids",
                        lambda: ({k: sorted(v) for k, v in state["scan"].items()},
                                 state["scan_ok"]))
    monkeypatch.setattr(transport, "_pid_alive", lambda pid: pid in state["live"])
    monkeypatch.setattr(transport, "ps_scan_supported", lambda: state["ps"])

    def _fake_stop(pid: int) -> str:
        state["killed"].append(pid)
        state["live"].discard(pid)
        return ""

    monkeypatch.setattr(dispatcher, "_stop_pid", _fake_stop)

    state["scan"][("gitlab-mr", "33311")] = [101, 102]
    state["live"].update({101, 102})
    (tmp_path / "supertool-watch-gitlab-mr__33311.pid").write_text("101\n")

    state["dir"] = tmp_path
    state["monkeypatch"] = monkeypatch
    return state


def _register(fleet, tier):
    fleet["monkeypatch"].setenv(radar.TIERS_ENV, '{"fake": {}}')
    fleet["monkeypatch"].setattr(radar, "_tier_module",
                                 lambda n: tier if n == "fake" else None)


def _spawning_tier(order: list[str]):
    """A tier that asks radar for one slot, the way gl-mrs asks for its feed."""
    def report(opts):
        order.append("report")
        opts["_watch"]("gitlab-mr", "33311")
        return ["board"], True
    return types.SimpleNamespace(RADAR_OPTIONS={"quiet_when_healthy"},
                                 RADAR_QUIET_DEFAULT=False, radar_report=report)


@pytest.fixture
def spawnable(fleet):
    """Spawning is faked and ordered, so `reap` vs `spawn` is observable."""
    order: list[str] = []
    monkeypatch = fleet["monkeypatch"]
    real_reap = dispatcher.reap_duplicate_pollers

    def _reap():
        order.append("reap")
        return real_reap()

    monkeypatch.setattr(dispatcher, "reap_duplicate_pollers", _reap)
    monkeypatch.setattr(dispatcher, "start_poller",
                        lambda source, wid, only: (order.append(f"spawn:{source}:{wid}"),
                                                   ("spawned", 4242))[1])
    monkeypatch.setattr(dispatcher, "_load_source", lambda name: object())
    monkeypatch.setattr(transport, "deaths", lambda *a: [])
    fleet["order"] = order
    return fleet


def test_a_tier_that_could_not_run_costs_you_no_processes(spawnable, capsys):
    """The sharpest case: live GitLab is unreachable, the tier raises before it
    can spawn anything, radar exits non-zero with no board — and today it has
    already stopped a poller on the way in."""
    def boom(opts):
        raise RuntimeError("gitlab unreachable")

    _register(spawnable, types.SimpleNamespace(RADAR_OPTIONS=set(), radar_report=boom))

    assert radar.main([]) == 1
    capsys.readouterr()

    assert spawnable["killed"] == []
    assert "reap" not in spawnable["order"]


def test_a_run_whose_tiers_spawn_nothing_stops_nothing(spawnable, capsys):
    """A fleet-report tier keeps no watchers. Nothing about rendering its board
    licenses stopping someone else's poller."""
    _register(spawnable, types.SimpleNamespace(
        RADAR_OPTIONS={"quiet_when_healthy"}, RADAR_QUIET_DEFAULT=False,
        radar_report=lambda opts: (["FLEET — ok"], True)))

    assert radar.main([]) == 0
    out = capsys.readouterr().out

    assert spawnable["killed"] == []
    assert "102" not in out
    assert out.splitlines() == ["FLEET — ok"]


def test_a_run_that_spawns_still_reaps_and_still_reaps_first(spawnable, capsys):
    """The healing half stays. Reaping is radar's job on the path that keeps
    the fleet, and it must land before this run's own first spawn or it would
    be judging the poller it just started (#749)."""
    order = spawnable["order"]
    _register(spawnable, _spawning_tier(order))

    assert radar.main([]) == 0
    out = capsys.readouterr().out

    assert spawnable["killed"] == [102]
    assert order == ["report", "reap", "spawn:gitlab-mr:33311"]
    assert "102" in out and "gitlab-mr:33311" in out


def test_the_reap_runs_once_however_many_slots_a_tier_asks_for(spawnable):
    def report(opts):
        opts["_watch"]("gitlab-mr", "33311")
        opts["_watch"]("gitlab-mr", "33312")
        return [], True

    _register(spawnable, types.SimpleNamespace(RADAR_OPTIONS=set(),
                                               radar_report=report))
    radar.main([])

    assert spawnable["order"].count("reap") == 1


def test_the_reap_line_leads_the_board(spawnable, capsys):
    """"I stopped one of your processes" is not a footnote under the board."""
    order = spawnable["order"]
    _register(spawnable, _spawning_tier(order))

    radar.main([])

    first = capsys.readouterr().out.splitlines()[0]
    assert "102" in first


def test_state_stops_nothing(spawnable, capsys):
    """A pin, green before this change too: `--state` returns above the reap.
    It is here so the read-only contract survives the reap moving."""
    _register(spawnable, types.SimpleNamespace(RADAR_OPTIONS=set(),
                                               radar_state=lambda opts: ["  x : y"]))

    radar.main(["radar.py", "--state"])
    capsys.readouterr()

    assert spawnable["killed"] == []
