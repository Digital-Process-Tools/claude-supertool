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

    def __init__(self, mine: Path, theirs: Path, base: Path) -> None:
        self.mine = str(mine)
        self.theirs = str(theirs)
        self.base = base
        self.rows: list[tuple[int, list[str]]] = []
        self.alive: set[int] = set()
        self.stopped: list[int] = []
        self.scan_breaks = False

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

    def add_resolvable_channel(self, name: str, pid: int, source: str,
                                watcher_id: str) -> str:
        """A foreign channel `channel_dirs()` can actually hash back to a path.

        `self.theirs` deliberately cannot be resolved -- it is named `default`
        and so carries none of the `supertool-watch-` prefix `channel_dirs()`
        keys on -- which is why every test above it exercises only the
        "no state directory hashes to it" arm. This builds the other one.
        """
        state_dir = self.base / f"supertool-watch-{name}"
        state_dir.mkdir()
        self.rows.append(
            (pid, self.argv_under(str(state_dir), source, watcher_id)))
        self.alive.add(pid)
        return str(state_dir)

    def ps_rows(self):
        if self.scan_breaks:
            return None
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
    # `channel_dirs()` reads `naming.BASE_DIR` at call time and lists it. Left
    # at the real "/tmp", a `supertool-watch-*` directory belonging to whoever
    # is running the suite lands in the token map, so what this file asserts
    # about a foreign channel would depend on the developer's own machine.
    monkeypatch.setattr(naming, "BASE_DIR", str(tmp_path))
    m = _Machine(mine, theirs, tmp_path)
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


def test_unwatch_names_the_foreign_channels_state_dir_when_it_resolves(
        machine, capsys) -> None:
    """The actionable half of the disclosure, and the half nothing covered.

    `channel_dirs()` exists to hash *forward* over the directories under
    BASE_DIR so a channel token -- which is a one-way `sha256(...)[:12]` and
    reverses into nothing -- turns back into the path, and so into the
    `SUPERTOOL_WATCH_NAME` whose own board can stop the poller. That is the
    whole of #1881's complaint: five slots, 564 processes, and no route from
    the token to the thing that could act on them. The disclosure's closing
    line tells the operator to "run `unwatch` under the SUPERTOOL_WATCH_NAME
    that derives their state dir", which is advice they cannot follow if the
    state dir was never named.

    Every other test in this file uses `add_theirs`, whose directory is named
    `default` and therefore never matches the `supertool-watch-` prefix
    `channel_dirs()` keys on -- so all of them land on the "no state directory
    hashes to it" arm and the resolved one went unexercised. Rendering goes
    through `naming.flat_path`, never a raw interpolation (#1522), so that is
    what this asserts against.
    """
    state_dir = machine.add_resolvable_channel("theirs", 202, *SLOT)
    assert dispatcher.cmd_unwatch(list(SLOT)) == 0
    out = capsys.readouterr().out
    assert "another channel" in out, out
    assert f"state dir {naming.flat_path(state_dir)}" in out, out
    # The two arms are mutually exclusive: a resolved channel must not also
    # report itself unresolvable, which is what would happen if the lookup
    # silently missed and the fallback carried the line.
    assert "hashes to it" not in out, out
    assert "could not be resolved" not in out, out
    assert "202" not in out, out  # still a count, never a PID


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


# ---------------------------------------------------------------------------
# a scan that could not run must not render as "no foreign poller found"
# ---------------------------------------------------------------------------

def test_unwatch_discloses_an_unavailable_scan_on_the_tracked_but_dead_branch(
        machine, capsys) -> None:
    """The auditor's finding: two of the three "nothing stopped" branches
    called `_foreign_slot_lines`, which returns `[]` both when the scan found
    nothing AND when the scan never ran -- so a failed scan on this branch
    read exactly like a slot nobody else is covering. Only the third branch
    (a bare "no PID file, no process") already said so.
    """
    tracked = machine.add_mine(101, *SLOT)
    transport.record_pid(*SLOT, tracked)
    machine.alive.discard(tracked)  # tracked PID is now dead
    machine.scan_breaks = True
    assert dispatcher.cmd_unwatch(list(SLOT)) == 0
    out = capsys.readouterr().out
    assert "is not running" in out, out  # the tracked-but-dead sentence itself
    # Hedged, not claimed: the scan never ran, so nothing here may assert a
    # foreign poller exists -- only that one could not be ruled out.
    assert "also saw poller" not in out, out  # the confirmed-disclosure header
    assert "scan" in out.lower() and (
        "unavailable" in out.lower() or "could not" in out.lower()), out

    # must-fire, same fixture: a working scan on the identical dead-tracked
    # slot must NOT print the scan-unavailable line -- without this the
    # assertion above would pass on a line printed unconditionally.
    machine.scan_breaks = False
    tracked2 = machine.add_mine(202, "gitlab-mr", "99999")
    transport.record_pid("gitlab-mr", "99999", tracked2)
    machine.alive.discard(tracked2)
    assert dispatcher.cmd_unwatch(["gitlab-mr", "99999"]) == 0
    out2 = capsys.readouterr().out
    assert "scan" not in out2.lower() or "unavailable" not in out2.lower(), out2


# ---------------------------------------------------------------------------
# a poller whose channel is unknowable is not "another channel"
# ---------------------------------------------------------------------------

def test_unwatch_does_not_call_an_unlabelled_poller_another_channels(
        machine, capsys) -> None:
    """The auditor's second finding: the disclosure header asserted "on
    another channel" even when the only foreign-looking poller found was one
    whose argv predates the channel token -- a poller `poller_census` cannot
    place on any channel, including possibly this one.
    """
    pid = 909
    machine.rows.append((pid, [
        sys.executable, str(WATCH_DIR / "dispatcher.py"),
        transport.POLL_SUBOP, *SLOT,
    ]))
    machine.alive.add(pid)
    assert dispatcher.cmd_unwatch(list(SLOT)) == 0
    out = capsys.readouterr().out
    assert "cannot be told" in out, out
    assert "on another channel" not in out, out


def test_the_change_is_findable():
    assert_change_is_findable(1893)
