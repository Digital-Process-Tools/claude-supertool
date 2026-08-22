"""#1893 -- `unwatch` reaching zero of this channel's pollers says nothing about
another channel's live pollers on the same slot, which is #1890's defect one
surface over.

`cmd_unwatch` and `reap_duplicate_pollers` are correct to act only on this
channel (#1514) -- this issue is not about widening what either kills. It is
about `cmd_unwatch`'s own "nothing to stop" report: an operator whose slot is
covered by another channel's poller runs `unwatch`, sees no error, and the
poller keeps running with nothing on the board explaining why. #1890 fixed the
identical reading in `watches`; this fixes it in the one surface that was
deliberately left alone at the time.

Nothing here spawns, signals or reaps a real process. The process table is a
list, liveness is a set, and `_stop_pid` is recorded rather than run.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from _changelog_findable import assert_change_is_findable

WATCH_DIR = Path(__file__).parent.parent / "presets" / "watch"
sys.path.insert(0, str(WATCH_DIR))

import naming  # noqa: E402
import transport  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dispatcher = _load("watch_dispatcher_1893", WATCH_DIR / "dispatcher.py")

SLOT = ("gitlab-mr", "33698")


class _Machine:
    """A process table, a liveness set, and a stop that only records."""

    def __init__(self, mine: Path, theirs: Path) -> None:
        self.mine = str(mine)
        self.theirs = str(theirs)
        self.rows: list[tuple[int, list[str]]] = []
        self.alive: set[int] = set()
        self.stopped: list[int] = []

    def argv_under(self, state_dir: str, source: str, watcher_id: str) -> list[str]:
        saved = transport.STATE_DIR
        transport.STATE_DIR = state_dir
        try:
            return transport.poller_argv(source, watcher_id, [])
        finally:
            transport.STATE_DIR = saved

    def add_mine(self, pid: int, source: str, watcher_id: str) -> int:
        self.rows.append((pid, self.argv_under(self.mine, source, watcher_id)))
        self.alive.add(pid)
        return pid

    def add_theirs(self, pid: int, source: str, watcher_id: str) -> int:
        self.rows.append((pid, self.argv_under(self.theirs, source, watcher_id)))
        self.alive.add(pid)
        return pid

    def ps_rows(self):
        return [(pid, argv) for pid, argv in self.rows if pid in self.alive]

    def pid_alive(self, pid: int) -> bool:
        return pid in self.alive

    def stop(self, pid: int) -> str:
        self.stopped.append(pid)
        self.alive.discard(pid)
        return ""


@pytest.fixture
def machine(tmp_path, monkeypatch) -> _Machine:
    mine = tmp_path / "supertool-watch-oss-supertool"
    theirs = tmp_path / "default"
    mine.mkdir()
    theirs.mkdir()
    monkeypatch.setattr(transport, "STATE_DIR", str(mine))
    monkeypatch.setattr(dispatcher.transport, "STATE_DIR", str(mine))
    m = _Machine(mine, theirs)
    monkeypatch.setattr(transport, "_ps_rows", m.ps_rows)
    monkeypatch.setattr(transport, "_pid_alive", m.pid_alive)
    monkeypatch.setattr(dispatcher, "_stop_pid", m.stop)
    monkeypatch.setattr(transport, "ps_scan_supported", lambda: True)
    return m


def test_unwatch_discloses_a_foreign_poller_on_the_same_slot(machine, capsys) -> None:
    """The #1893 symptom: unwatch reaches zero of its own and says nothing."""
    theirs = machine.add_theirs(202, *SLOT)
    assert dispatcher.cmd_unwatch(list(SLOT)) == 0
    out = capsys.readouterr().out
    assert "No active watcher" in out, out
    assert theirs in machine.alive
    assert machine.stopped == []
    # Disclosed, not acted on: the operator can now tell "nothing here" apart
    # from "something here, but not on this channel".
    assert "another channel" in out, out
    assert str(theirs) not in out, out  # never a PID this channel may not act on


def test_unwatch_says_nothing_extra_when_no_foreign_poller_exists(
        machine, capsys) -> None:
    """The must-fire pair: a genuinely empty slot gets the plain sentence only.

    Without this, a disclosure line printed on every `unwatch` regardless of
    whether the census found anything would satisfy the assertion above for
    the wrong reason.
    """
    assert dispatcher.cmd_unwatch(list(SLOT)) == 0
    out = capsys.readouterr().out
    assert "No active watcher" in out, out
    assert "another channel" not in out, out


def test_unwatch_still_stops_its_own_poller_when_a_foreign_one_also_exists(
        machine, capsys) -> None:
    """The disclosure must not interfere with the normal multi-kill path."""
    mine = machine.add_mine(101, *SLOT)
    theirs = machine.add_theirs(202, *SLOT)
    transport.record_pid(*SLOT, mine)
    assert dispatcher.cmd_unwatch(list(SLOT)) == 0
    assert machine.stopped == [mine]
    assert theirs in machine.alive


def test_the_change_is_findable():
    assert_change_is_findable(1893)
