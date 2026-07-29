"""#513 — a watcher that dies must keep saying so; a deliberate stop must not.

`watches` unlinked a dead poller's stale PID file and dropped the row, so an id
that *had* coverage and lost it rendered byte-identically to one that never had
any. That is the worst member of this repository's recurring family, because the
thing going quiet is the monitoring surface itself: a validator that declines
costs one check, a radar that lost a watcher costs every event on that MR while
the board keeps rendering as though coverage were complete.

The distinguisher is not a heuristic, it is the artifact each exit leaves behind:

  | exit                     | pid file           | state file |
  | ------------------------ | ------------------ | ---------- |
  | terminal (merged/closed) | released by poller | cleared    |
  | deliberate `unwatch`     | released           | kept       |
  | death (SIGKILL/crash)    | **stale, PID dead**| kept       |

So a pid file naming a dead process is a death and nothing else is — and the
ledger lives *in the state file*, which a terminal exit deletes outright. A
terminal watcher therefore cannot be reported as a loss by construction rather
than by a check that could drift.

Nothing here spawns or signals a real process. The process table is a fixture
and `os.kill` is recorded (the pattern #512's tests established) — the
maintainer's live radar fleet is not touched.
"""
from __future__ import annotations

import importlib.util
import json
import os
import signal
import sys
from pathlib import Path

import pytest

WATCH_DIR = Path(__file__).parent.parent / "presets" / "watch"
sys.path.insert(0, str(WATCH_DIR))

import transport  # noqa: E402  (the same module object dispatcher imports)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dispatcher = _load("watch_dispatcher_513", WATCH_DIR / "dispatcher.py")
radar = _load("watch_radar_513", WATCH_DIR / "radar.py")

DEAD = 4252000  # a PID the fixture never marks alive


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    """Every pid/state path under the test's own dir, never the real /tmp."""
    for mod in (transport, dispatcher.transport, radar.transport):
        monkeypatch.setattr(mod, "STATE_DIR", str(tmp_path))
    return tmp_path


class _Machine:
    """A fake process table. `alive` is the truth, the pidfile is the belief."""

    def __init__(self) -> None:
        self.rows: list[tuple[int, list[str]]] = []
        self.alive: set[int] = set()
        self.killed: list[tuple[int, int]] = []
        self.scan_ok = True

    def add_poller(self, pid: int, source: str, watcher_id: str) -> int:
        self.rows.append((pid, transport.poller_argv(source, watcher_id, [])))
        self.alive.add(pid)
        return pid

    def add_unlabelled(self, pid: int) -> int:
        """A poller spawned before #512: it wears its parent's argv."""
        self.rows.append((pid, ["python3", str(WATCH_DIR / "radar.py"), "author=@me"]))
        self.alive.add(pid)
        return pid

    def ps_rows(self):
        if not self.scan_ok:
            return None
        return [(pid, argv) for pid, argv in self.rows if pid in self.alive]

    def pid_alive(self, pid: int) -> bool:
        return pid in self.alive

    def kill(self, pid: int, sig: int) -> None:
        self.killed.append((pid, sig))
        if pid not in self.alive:
            raise ProcessLookupError(f"no such process {pid}")
        self.alive.discard(pid)

    def install(self, monkeypatch) -> "_Machine":
        monkeypatch.setattr(transport, "_ps_rows", self.ps_rows)
        monkeypatch.setattr(transport, "_pid_alive", self.pid_alive)
        monkeypatch.setattr(dispatcher.os, "kill", self.kill)
        monkeypatch.setattr(dispatcher.time, "sleep", lambda _s: None)
        return self


@pytest.fixture
def machine(monkeypatch) -> _Machine:
    return _Machine().install(monkeypatch)


def _pidfile(tmp_path: Path, source: str, watcher_id: str, pid: int) -> Path:
    path = tmp_path / f"supertool-watch-{source}__{watcher_id}.pid"
    path.write_text(f"{pid}\n")
    return path


def _statefile(tmp_path: Path, source: str, watcher_id: str, body: dict) -> Path:
    path = tmp_path / f"supertool-watch-{source}__{watcher_id}.state.json"
    path.write_text(json.dumps(body))
    return path


# ---------------------------------------------------------------------------
# the ledger — a death is recorded where a terminal exit cannot leave one
# ---------------------------------------------------------------------------

def test_a_stale_pidfile_records_a_death(machine, state_dir) -> None:
    _pidfile(state_dir, "gitlab-mr", "33248", DEAD)
    assert transport.reap_dead_pidfile("gitlab-mr", "33248") == DEAD
    recorded = transport.deaths("gitlab-mr", "33248")
    assert [d["pid"] for d in recorded] == [DEAD]
    assert not os.path.exists(transport.pid_path("gitlab-mr", "33248"))


def test_a_live_pidfile_records_nothing(machine, state_dir) -> None:
    machine.add_poller(101, "gitlab-mr", "33248")
    _pidfile(state_dir, "gitlab-mr", "33248", 101)
    assert transport.reap_dead_pidfile("gitlab-mr", "33248") == 0
    assert transport.deaths("gitlab-mr", "33248") == []


def test_the_same_dead_pid_is_recorded_once(machine, state_dir) -> None:
    """Two reapers race on one corpse — `watches` and radar's heal both reap."""
    _pidfile(state_dir, "gitlab-mr", "33248", DEAD)
    transport.record_death("gitlab-mr", "33248", DEAD)
    transport.record_death("gitlab-mr", "33248", DEAD)
    assert len(transport.deaths("gitlab-mr", "33248")) == 1


def test_clearing_the_state_file_clears_the_ledger(machine, state_dir) -> None:
    """The structural reason a terminal exit can never be reported as a loss."""
    transport.record_death("gitlab-mr", "33248", DEAD)
    assert transport.deaths("gitlab-mr", "33248")
    transport.clear_state("gitlab-mr", "33248")
    assert transport.deaths("gitlab-mr", "33248") == []


# ---------------------------------------------------------------------------
# `watches` — the passive surface, the one a session actually reads
# ---------------------------------------------------------------------------

def test_watches_keeps_the_row_of_a_watcher_that_died(machine, state_dir) -> None:
    """The bug. The row used to be unlinked and dropped, so lost == never had."""
    _pidfile(state_dir, "gitlab-mr", "33248", DEAD)
    _statefile(state_dir, "gitlab-mr", "33248", {"source_state": {"pipeline": "failed"}})
    rows, scan_ok = transport.list_watchers()
    assert scan_ok is True
    dead = [r for r in rows if r["id"] == "33248"]
    assert len(dead) == 1
    assert dead[0]["dead"] is True
    assert [d["pid"] for d in dead[0]["deaths"]] == [DEAD]


def test_watches_names_the_dead_watcher_on_the_board(machine, state_dir, capsys) -> None:
    _pidfile(state_dir, "gitlab-mr", "33248", DEAD)
    assert dispatcher.cmd_list() == 0
    out = capsys.readouterr().out
    assert "33248" in out
    assert str(DEAD) in out
    assert "died" in out.lower()


def test_a_slot_that_lost_its_poller_keeps_saying_so_on_every_run(
    machine, state_dir, capsys,
) -> None:
    """Not a print statement: the second read must say it too, unprompted."""
    _pidfile(state_dir, "gitlab-mr", "33248", DEAD)
    dispatcher.cmd_list()
    capsys.readouterr()
    dispatcher.cmd_list()
    assert "died" in capsys.readouterr().out.lower()


def test_a_healthy_watcher_is_not_reported_as_a_loss(machine, state_dir, capsys) -> None:
    machine.add_poller(101, "gitlab-mr", "33248")
    _pidfile(state_dir, "gitlab-mr", "33248", 101)
    dispatcher.cmd_list()
    out = capsys.readouterr().out
    assert "101" in out
    assert "died" not in out.lower()


def test_a_pre_512_unlabelled_poller_is_alive_not_dead(machine, state_dir, capsys) -> None:
    """It is invisible to the scan by construction. That is not a death.

    Deaths are derived from the pid file only; the labelled-process scan never
    contributes evidence of one. Otherwise every watcher the maintainer is
    running right now would be reported as lost the moment this lands.
    """
    pid = machine.add_unlabelled(777)
    _pidfile(state_dir, "gitlab-mr", "33248", pid)
    rows, _ = transport.list_watchers()
    row = [r for r in rows if r["id"] == "33248"][0]
    assert row["dead"] is False
    assert transport.deaths("gitlab-mr", "33248") == []
    dispatcher.cmd_list()
    assert "died" not in capsys.readouterr().out.lower()


def test_an_unavailable_scan_is_not_evidence_of_death(machine, state_dir) -> None:
    machine.add_poller(101, "gitlab-mr", "33248")
    _pidfile(state_dir, "gitlab-mr", "33248", 101)
    machine.scan_ok = False
    rows, scan_ok = transport.list_watchers()
    assert scan_ok is False
    assert [r for r in rows if r["id"] == "33248"][0]["dead"] is False
    assert transport.deaths("gitlab-mr", "33248") == []


# ---------------------------------------------------------------------------
# what a death is *not* — the two legitimate exits
# ---------------------------------------------------------------------------

def test_a_deliberate_unwatch_is_not_a_death(machine, state_dir, capsys) -> None:
    """Otherwise the board grows a permanent red row for every intentional stop,
    people learn to skim it, and a real red gets missed — #511's opening line."""
    machine.add_poller(101, "gitlab-mr", "33248")
    _pidfile(state_dir, "gitlab-mr", "33248", 101)
    _statefile(state_dir, "gitlab-mr", "33248", {"source_state": {"pipeline": "failed"}})
    assert dispatcher.cmd_unwatch(["gitlab-mr", "33248"]) == 0
    capsys.readouterr()
    assert transport.deaths("gitlab-mr", "33248") == []
    dispatcher.cmd_list()
    assert "died" not in capsys.readouterr().out.lower()


def test_unwatch_acknowledges_a_death_already_on_record(machine, state_dir, capsys) -> None:
    """`unwatch` on a slot that lost its poller is the operator saying "seen"."""
    _pidfile(state_dir, "gitlab-mr", "33248", DEAD)
    transport.reap_dead_pidfile("gitlab-mr", "33248")
    assert transport.deaths("gitlab-mr", "33248")
    assert dispatcher.cmd_unwatch(["gitlab-mr", "33248"]) == 0
    assert transport.deaths("gitlab-mr", "33248") == []
    capsys.readouterr()
    dispatcher.cmd_list()
    assert "died" not in capsys.readouterr().out.lower()


def test_a_terminal_exit_leaves_no_death_behind(machine, state_dir, monkeypatch, capsys) -> None:
    """A poller exits by design on merged/closed. That is not a loss.

    Driven through the real poll loop rather than asserted about it: the loop's
    terminal branch clears the state file, and the ledger lives in the state
    file, so there is nothing left for a reaper to find.
    """
    class _Terminal:
        INTERVAL = 0

        def poll(self, state, ctx):
            return [], {"state": "merged"}

        def is_terminal(self, state):
            return state.get("state") == "merged"

    monkeypatch.setattr(dispatcher, "_load_source", lambda name: _Terminal())
    monkeypatch.setattr(dispatcher, "_silence_stdio", lambda: None)
    machine.alive.add(os.getpid())
    dispatcher._run_poll_loop("gitlab-mr", "33248", [])

    assert not os.path.exists(transport.pid_path("gitlab-mr", "33248"))
    assert transport.deaths("gitlab-mr", "33248") == []
    rows, _ = transport.list_watchers()
    assert [r for r in rows if r["id"] == "33248"] == []
    dispatcher.cmd_list()
    assert "died" not in capsys.readouterr().out.lower()


# ---------------------------------------------------------------------------
# radar — heal, but bounded, and the bound has to speak
# ---------------------------------------------------------------------------

@pytest.fixture
def radar_env(state_dir, monkeypatch, machine):
    """Radar with a recording spawn. Nothing forks."""
    spawned: list[tuple[str, str, list[str]]] = []

    def _fake_spawn(source, watcher_id, only):
        spawned.append((source, watcher_id, list(only)))
        machine.alive.add(os.getpid())
        return os.getpid()

    monkeypatch.setattr(radar.dispatcher, "_spawn_poller", _fake_spawn)
    monkeypatch.setattr(radar.transport, "_pid_alive", machine.pid_alive)
    return {"dir": state_dir, "spawned": spawned, "monkeypatch": monkeypatch,
            "machine": machine}


def test_radar_heals_a_watcher_that_died_and_names_the_loss(radar_env) -> None:
    _pidfile(radar_env["dir"], radar.SOURCE, "33161", DEAD)
    healed, uncovered, refused = radar.heal(["33161"], set())
    assert healed == ["33161"]
    assert uncovered == [] and refused == []
    assert [d["pid"] for d in transport.deaths(radar.SOURCE, "33161")] == [DEAD]


def test_a_repeatedly_dying_watcher_stops_being_respawned(radar_env) -> None:
    """Silently respawning forever converts a visible failure into an invisible
    loop — this bug wearing a different hat."""
    limit = transport.DEATH_RESPAWN_LIMIT
    for n in range(limit):
        _pidfile(radar_env["dir"], radar.SOURCE, "33161", DEAD + n)
        healed, uncovered, refused = radar.heal(["33161"], set())
        if n < limit - 1:
            assert healed == ["33161"], f"death {n + 1} should still heal"
            transport.release_pidfile(radar.SOURCE, "33161")
    assert healed == []
    assert refused == ["33161"]
    assert uncovered == ["33161"], "a refused slot is uncovered, not quietly fine"
    assert [s for s in radar_env["spawned"] if s[1] == "33161"] == [
        (radar.SOURCE, "33161", s[2]) for s in radar_env["spawned"] if s[1] == "33161"
    ]
    assert len([s for s in radar_env["spawned"] if s[1] == "33161"]) == limit - 1


def test_the_board_says_why_it_stopped_respawning(radar_env, capsys) -> None:
    """A cap that stops quietly is the same silence one level up."""
    _statefile(radar_env["dir"], radar.SOURCE, "33161", {
        "deaths": [{"pid": DEAD + n, "ts": "2026-07-29T09:0%d:00Z" % n}
                   for n in range(transport.DEATH_RESPAWN_LIMIT)],
    })
    radar_env["monkeypatch"].setattr(
        radar, "live_open_mrs",
        lambda multi=None: [{"iid": 33161, "title": "t", "_pipeline": "failed",
                             "web_url": "", "source_branch": "b", "author": {},
                             "_pipeline_id": "1"}],
    )
    assert radar.main([]) == 0
    out = capsys.readouterr().out
    assert "33161" in out
    assert "not respawning" in out.lower()
    assert "watch:gitlab-mr:33161" in out


def test_re_arming_by_hand_clears_the_refusal(radar_env, capsys, monkeypatch) -> None:
    """The cap has to have a door out, and it is an explicit operator action."""
    _statefile(radar_env["dir"], radar.SOURCE, "33161", {
        "deaths": [{"pid": DEAD + n, "ts": "x"}
                   for n in range(transport.DEATH_RESPAWN_LIMIT)],
    })
    assert radar.heal(["33161"], set())[2] == ["33161"]
    monkeypatch.setattr(dispatcher, "_spawn_poller",
                        lambda s, i, o: radar_env["machine"].alive.add(999) or 999)
    assert dispatcher.cmd_watch(["gitlab-mr", "33161"]) == 0
    capsys.readouterr()
    assert transport.deaths(radar.SOURCE, "33161") == []


def test_a_slot_healed_once_does_not_stay_red_forever(radar_env, capsys) -> None:
    """One death that healed cleanly is reported on the run that healed it and
    then goes quiet. A permanent warning on a covered slot is what trains a
    reader to skim the board."""
    _pidfile(radar_env["dir"], radar.SOURCE, "33161", DEAD)
    radar.heal(["33161"], set())
    dispatcher.cmd_list()
    out = capsys.readouterr().out
    assert "33161" in out
    assert "died" not in out.lower()
