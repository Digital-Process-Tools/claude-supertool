"""#1514 — the poller label carries (source, id) but not the channel.

`scan_poller_pids` matches pollers by the argv `transport.poller_argv` writes,
and that label had no channel dimension. So a `watches` run under
`SUPERTOOL_WATCH_NAME=oss-supertool` enumerated *every* labelled poller on the
machine, found no pid file for the default channel's ones under its own state
directory, and listed four of them as its own `no pidfile` orphans — the exact
column an operator acts on, offering `unwatch:SOURCE:ID` against a watcher
belonging to somebody else's channel.

The sharper half, which the issue left open: `radar`'s reap reads the same scan.
Two channels each running one poller for the same slot grouped as one slot with
two pids, and the reap stopped the one this channel's pid file did not name.
That is a cross-channel *kill*, not a cross-channel listing.

The slot is the pid file, and the pid file lives in `STATE_DIR` — so the channel
a poller belongs to is the state directory it claimed its slot in, and that is
what the label now carries. Three states come back out of `_labelled`: this
channel's, another channel's, and a poller whose channel cannot be told at all
(one started before this label existed). Only the first is ever acted on.

Nothing here spawns, signals or reaps a real process. The process table is a
list, liveness is a set, and `_stop_pid` is recorded.
"""
from __future__ import annotations

import importlib.util
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


dispatcher = _load("watch_dispatcher_1514", WATCH_DIR / "dispatcher.py")

SLOT = ("gitlab-mr", "33698")


class _Machine:
    """A process table, a liveness set, and a stop that only records."""

    def __init__(self, mine: Path, theirs: Path) -> None:
        self.mine = str(mine)
        self.theirs = str(theirs)
        self.rows: list[tuple[int, list[str]]] = []
        self.alive: set[int] = set()
        self.stopped: list[int] = []

    def argv_under(self, state_dir: str, source: str, watcher_id: str,
                   only=None) -> list[str]:
        """`poller_argv` as a process running in `state_dir` would have written it."""
        saved = transport.STATE_DIR
        transport.STATE_DIR = state_dir
        try:
            return transport.poller_argv(source, watcher_id, only or [])
        finally:
            transport.STATE_DIR = saved

    def add_mine(self, pid: int, source: str, watcher_id: str, only=None) -> int:
        self.rows.append((pid, self.argv_under(self.mine, source, watcher_id, only)))
        self.alive.add(pid)
        return pid

    def add_theirs(self, pid: int, source: str, watcher_id: str, only=None) -> int:
        self.rows.append((pid, self.argv_under(self.theirs, source, watcher_id, only)))
        self.alive.add(pid)
        return pid

    def add_unlabelled(self, pid: int, source: str, watcher_id: str) -> int:
        """A poller started before the channel token existed: the #511 shape."""
        self.rows.append((pid, [
            sys.executable,
            str(WATCH_DIR / "dispatcher.py"),
            transport.POLL_SUBOP,
            source,
            watcher_id,
        ]))
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


# ---------------------------------------------------------------------------
# the label
# ---------------------------------------------------------------------------

def test_the_label_carries_the_channel(machine) -> None:
    """Same slot, two state directories, two different command lines."""
    mine = machine.argv_under(machine.mine, *SLOT)
    theirs = machine.argv_under(machine.theirs, *SLOT)
    assert mine != theirs


def test_the_label_round_trips_through_the_matcher(machine) -> None:
    argv = transport.poller_argv(*SLOT, [])
    assert transport._labelled(argv) == (transport.channel_key(), *SLOT)


def test_a_poller_with_no_channel_token_reads_as_unknown(machine) -> None:
    """Not this channel's, not another's — the third state (#511's population)."""
    argv = [sys.executable, str(WATCH_DIR / "dispatcher.py"),
            transport.POLL_SUBOP, *SLOT]
    assert transport._labelled(argv) == (None, *SLOT)


def test_the_labelled_argv_is_still_a_command_the_dispatcher_can_run(
        machine, monkeypatch) -> None:
    """A label the dispatcher cannot parse back would exit every watcher it named."""
    ran: list[tuple[str, str, list[str]]] = []
    monkeypatch.setattr(dispatcher, "_run_poll_loop",
                        lambda s, i, o: ran.append((s, i, list(o))))
    argv = transport.poller_argv("gitlab-mr", "33248", ["pipeline_failed"])
    assert dispatcher.main(argv[1:]) == 0
    assert ran == [("gitlab-mr", "33248", ["pipeline_failed"])]


# ---------------------------------------------------------------------------
# the scan
# ---------------------------------------------------------------------------

def test_this_channels_pollers_are_still_found(machine) -> None:
    machine.add_mine(101, *SLOT)
    found, scan_ok = transport.scan_poller_pids()
    assert scan_ok
    assert found == {SLOT: [101]}


def test_another_channels_poller_is_not_in_this_channels_scan(machine) -> None:
    machine.add_theirs(202, *SLOT)
    found, scan_ok = transport.scan_poller_pids()
    assert scan_ok
    assert found == {}


def test_a_poller_with_no_channel_token_is_not_claimed_by_this_channel(machine) -> None:
    machine.add_unlabelled(303, *SLOT)
    found, _ = transport.scan_poller_pids()
    assert found == {}


# ---------------------------------------------------------------------------
# the board — the render the issue was filed against
# ---------------------------------------------------------------------------

def test_another_channels_poller_is_not_this_channels_orphan(machine, capsys) -> None:
    """The live render in #1514: four `no pidfile` rows belonging elsewhere."""
    machine.add_theirs(202, *SLOT)
    machine.add_theirs(204, "gl-runners", "fleet")
    rows, scan_ok = transport.list_watchers()
    assert scan_ok
    assert rows == []
    assert dispatcher.cmd_list() == 0
    out = capsys.readouterr().out
    assert "no pidfile" not in out
    assert "33698" not in out


# ---------------------------------------------------------------------------
# the reap — a cross-channel kill, which the listing bug was hiding
# ---------------------------------------------------------------------------

def test_the_reap_does_not_stop_another_channels_poller(machine) -> None:
    """One poller per channel on the same slot is not two pollers on one slot."""
    mine = machine.add_mine(101, *SLOT)
    theirs = machine.add_theirs(202, *SLOT)
    transport.record_pid(*SLOT, mine)
    assert dispatcher.reap_duplicate_pollers() == []
    assert machine.stopped == []
    assert theirs in machine.alive


def test_the_reap_still_stops_a_duplicate_inside_this_channel(machine) -> None:
    keep = machine.add_mine(101, *SLOT)
    surplus = machine.add_mine(102, *SLOT)
    transport.record_pid(*SLOT, keep)
    lines = dispatcher.reap_duplicate_pollers()
    assert machine.stopped == [surplus]
    assert lines and str(surplus) in lines[0]


# ---------------------------------------------------------------------------
# unwatch — the command the board was offering against somebody else's watcher
# ---------------------------------------------------------------------------

def test_unwatch_does_not_stop_another_channels_poller(machine, capsys) -> None:
    theirs = machine.add_theirs(202, *SLOT)
    assert dispatcher.cmd_unwatch(list(SLOT)) == 0
    assert machine.stopped == []
    assert theirs in machine.alive
    assert "No active watcher" in capsys.readouterr().out
