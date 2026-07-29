"""#511 — one watcher id, several live pollers: see all of them, reach all of them.

The state model was one `{id -> pid}` mapping, so a second poller on the same id
was not merely untracked, it was *unreachable*: `watches` showed one row,
`unwatch` killed the one PID it knew, the survivors kept emitting, and the next
`unwatch` answered "No active watcher" while the state file was still being
rewritten every tick. Observed live: a single `mr_opened` arriving 3x, then 9x,
then 13x in four seconds.

Three defects, one shape — the tool's bookkeeping asserting an absence that is
not true of the world:

  - extras are invisible because only one PID per id is tracked
  - a deleted pidfile makes a live poller permanently unreachable
  - a forked poller inherits the *parent's* argv, so every per-MR watcher shows
    the feed's command line; the maintainer read three such rows as duplicate
    feed pollers and killed two, and they were the watchers for two different
    MRs.

The last one is the load-bearing fix, not a cosmetic one: an argv that names its
own (source, id) is what lets the other two be found at all. So the poller is
re-exec'd under an argv it can be identified by, and everything else here reads
that argv back.

Nothing in this file spawns or kills a real process. The process table is a
fixture, `os.kill` is recorded, and the maintainer's live radar fleet is not
touched.
"""
from __future__ import annotations

import importlib.util
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


dispatcher = _load("watch_dispatcher_511", WATCH_DIR / "dispatcher.py")

SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    """Every pid/state path under the test's own dir, never the real /tmp."""
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(dispatcher.transport, "STATE_DIR", str(tmp_path))
    return tmp_path


class _Machine:
    """A fake process table, and a kill that really removes processes from it.

    `alive` is the truth; the pidfile is the tool's belief about it. Every test
    below is about the two disagreeing.
    """

    def __init__(self) -> None:
        self.rows: list[tuple[int, list[str]]] = []
        self.alive: set[int] = set()
        self.killed: list[tuple[int, int]] = []
        self.scan_ok = True
        self.refuse: dict[int, OSError] = {}

    def add_poller(self, pid: int, source: str, watcher_id: str, only=None) -> int:
        self.rows.append((pid, transport.poller_argv(source, watcher_id, only or [])))
        self.alive.add(pid)
        return pid

    def add_process(self, pid: int, argv: list[str]) -> int:
        self.rows.append((pid, argv))
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
        if pid in self.refuse:
            raise self.refuse[pid]
        if pid not in self.alive:
            raise ProcessLookupError(f"no such process {pid}")
        if sig in (signal.SIGTERM, SIGKILL):
            self.alive.discard(pid)

    @property
    def killed_pids(self) -> set[int]:
        return {pid for pid, _sig in self.killed}

    def install(self, monkeypatch) -> "_Machine":
        monkeypatch.setattr(transport, "_ps_rows", self.ps_rows)
        monkeypatch.setattr(transport, "_pid_alive", self.pid_alive)
        monkeypatch.setattr(dispatcher.os, "kill", self.kill)
        monkeypatch.setattr(dispatcher.time, "sleep", lambda _s: None)
        return self


@pytest.fixture
def machine(monkeypatch) -> _Machine:
    return _Machine().install(monkeypatch)


# ---------------------------------------------------------------------------
# the label — a poller's argv must name the poller, not its parent
# ---------------------------------------------------------------------------

def test_poller_argv_names_its_own_source_and_id() -> None:
    argv = transport.poller_argv("gitlab-mr", "33248", [])
    assert "gitlab-mr" in argv
    assert "33248" in argv
    assert any(a.replace("\\", "/").endswith("watch/dispatcher.py") for a in argv)


def test_two_watchers_do_not_share_a_command_line() -> None:
    """The wrong kill in #511: three `ps` rows, byte-identical, three watchers."""
    feed = transport.poller_argv("gitlab-mr-feed", "author=@me,state=opened", [])
    one = transport.poller_argv("gitlab-mr", "33248", [])
    two = transport.poller_argv("gitlab-mr", "33249", [])
    assert feed != one != two
    assert one != two


def test_the_labelled_argv_is_a_command_the_dispatcher_can_actually_run(monkeypatch) -> None:
    """A label that is not a runnable command would kill every watcher it named."""
    ran: list[tuple[str, str, list[str]]] = []
    monkeypatch.setattr(dispatcher, "_run_poll_loop",
                        lambda s, i, o: ran.append((s, i, list(o))))
    argv = transport.poller_argv("gitlab-mr", "33248", ["pipeline_failed", "mr_merged"])
    assert dispatcher.main(argv[1:]) == 0
    assert ran == [("gitlab-mr", "33248", ["pipeline_failed", "mr_merged"])]


# ---------------------------------------------------------------------------
# reading the label back
# ---------------------------------------------------------------------------

def test_scan_finds_every_poller_for_one_id(machine) -> None:
    machine.add_poller(101, "gitlab-mr", "33248")
    machine.add_poller(102, "gitlab-mr", "33248")
    found, scan_ok = transport.live_poller_pids("gitlab-mr", "33248")
    assert scan_ok is True
    assert found == [101, 102]


def test_scan_does_not_confuse_two_different_watchers(machine) -> None:
    machine.add_poller(101, "gitlab-mr", "33248")
    machine.add_poller(102, "gitlab-mr", "33249")
    machine.add_poller(103, "gitlab-mr-feed", "author=@me,state=opened")
    assert transport.live_poller_pids("gitlab-mr", "33248")[0] == [101]
    assert transport.live_poller_pids("gitlab-mr", "33249")[0] == [102]


def test_scan_ignores_processes_that_are_not_pollers(machine) -> None:
    """The parent `watch` invocation and a grep for it are not watchers."""
    machine.add_process(201, ["python3", "/x/presets/watch/dispatcher.py",
                              "watch", "gitlab-mr", "33248"])
    machine.add_process(202, ["grep", "dispatcher.py poll gitlab-mr 33248"])
    machine.add_process(203, ["python3", "/x/presets/watch/radar.py",
                              "author=@me,state=opened"])
    assert transport.live_poller_pids("gitlab-mr", "33248")[0] == []


def test_scan_reports_when_it_could_not_look(machine) -> None:
    """`ps` unavailable must not render as `there are no extras`."""
    machine.scan_ok = False
    found, scan_ok = transport.live_poller_pids("gitlab-mr", "33248")
    assert (found, scan_ok) == ([], False)


def test_watcher_pids_unions_the_tracked_pid_with_the_scan(machine) -> None:
    machine.add_poller(101, "gitlab-mr", "33248")
    machine.add_poller(102, "gitlab-mr", "33248")
    transport.record_pid("gitlab-mr", "33248", 101)
    info = transport.watcher_pids("gitlab-mr", "33248")
    assert info["pids"] == [101, 102]
    assert info["tracked"] == 101
    assert info["untracked"] == [102]


def test_watcher_pids_reports_a_tracked_pid_that_is_dead(machine) -> None:
    """#511's 42520/42544: tracked, dead, and the board silently blind on it."""
    transport.record_pid("gitlab-mr", "33248", 42520)
    info = transport.watcher_pids("gitlab-mr", "33248")
    assert info["tracked"] == 42520
    assert info["tracked_alive"] is False
    assert info["pids"] == []


# ---------------------------------------------------------------------------
# `watches` — the surface that has to be right when `ps` is not
# ---------------------------------------------------------------------------

def test_watches_shows_a_count_when_an_id_has_more_than_one_poller(machine, capsys) -> None:
    machine.add_poller(101, "gitlab-mr", "33248")
    machine.add_poller(102, "gitlab-mr", "33248")
    transport.record_pid("gitlab-mr", "33248", 101)
    assert dispatcher.cmd_list() == 0
    out = capsys.readouterr().out
    assert "101" in out and "102" in out
    assert "2" in out


def test_watches_shows_a_poller_whose_pidfile_was_deleted(machine, capsys) -> None:
    """Deleting the pidfile used to hide the process; now it is still listed."""
    machine.add_poller(101, "gitlab-mr", "33248")
    assert dispatcher.cmd_list() == 0
    out = capsys.readouterr().out
    assert "33248" in out
    assert "101" in out
    assert "no pidfile" in out.lower()


def test_watches_says_ps_cannot_identify_a_watcher_when_extras_exist(machine, capsys) -> None:
    machine.add_poller(101, "gitlab-mr", "33248")
    machine.add_poller(102, "gitlab-mr", "33248")
    transport.record_pid("gitlab-mr", "33248", 101)
    dispatcher.cmd_list()
    assert "unwatch" in capsys.readouterr().out


def test_watches_says_when_the_process_scan_was_unavailable(machine, capsys) -> None:
    machine.scan_ok = False
    machine.alive.add(101)
    transport.record_pid("gitlab-mr", "33248", 101)
    assert dispatcher.cmd_list() == 0
    out = capsys.readouterr().out.lower()
    assert "scan" in out and "unavail" in out


# ---------------------------------------------------------------------------
# `unwatch` — reach all of them, and say which
# ---------------------------------------------------------------------------

def test_unwatch_stops_every_poller_for_the_id(machine, capsys) -> None:
    machine.add_poller(101, "gitlab-mr", "33248")
    machine.add_poller(102, "gitlab-mr", "33248")
    transport.record_pid("gitlab-mr", "33248", 101)
    assert dispatcher.cmd_unwatch(["gitlab-mr", "33248"]) == 0
    assert machine.killed_pids == {101, 102}
    assert machine.alive == set()
    capsys.readouterr()


def test_unwatch_names_every_pid_it_is_about_to_stop(machine, capsys) -> None:
    """A multi-kill the operator cannot audit is worse than the bug it fixes."""
    machine.add_poller(101, "gitlab-mr", "33248")
    machine.add_poller(102, "gitlab-mr", "33248")
    transport.record_pid("gitlab-mr", "33248", 101)
    dispatcher.cmd_unwatch(["gitlab-mr", "33248"])
    out = capsys.readouterr().out
    assert "101" in out and "102" in out
    assert "untracked" in out


def test_unwatch_does_not_touch_another_watchers_poller(machine, capsys) -> None:
    machine.add_poller(101, "gitlab-mr", "33248")
    machine.add_poller(102, "gitlab-mr", "33249")
    machine.add_poller(103, "gitlab-mr-feed", "author=@me,state=opened")
    transport.record_pid("gitlab-mr", "33248", 101)
    dispatcher.cmd_unwatch(["gitlab-mr", "33248"])
    assert machine.killed_pids == {101}
    assert machine.alive == {102, 103}
    capsys.readouterr()


def test_unwatch_reaches_a_poller_whose_pidfile_was_deleted(machine, capsys) -> None:
    """Today's only recovery for this is `pkill`."""
    machine.add_poller(101, "gitlab-mr", "33248")
    assert dispatcher.cmd_unwatch(["gitlab-mr", "33248"]) == 0
    assert machine.killed_pids == {101}
    capsys.readouterr()


def test_unwatch_reports_a_tracked_pid_that_had_already_died(machine, capsys) -> None:
    transport.record_pid("gitlab-mr", "33248", 42520)
    assert dispatcher.cmd_unwatch(["gitlab-mr", "33248"]) == 0
    assert machine.killed == []
    out = capsys.readouterr().out
    assert "42520" in out
    assert "not running" in out.lower()


def test_unwatch_on_nothing_says_it_checked_the_processes_too(machine, capsys) -> None:
    assert dispatcher.cmd_unwatch(["gitlab-mr", "33248"]) == 0
    out = capsys.readouterr().out.lower()
    assert "no active watcher" in out
    assert "process" in out


def test_unwatch_will_not_claim_an_absence_it_could_not_verify(machine, capsys) -> None:
    """No pidfile *and* no scan is not the same answer as no watcher."""
    machine.scan_ok = False
    assert dispatcher.cmd_unwatch(["gitlab-mr", "33248"]) == 0
    out = capsys.readouterr().out.lower()
    assert "unavail" in out
    assert "no active watcher" not in out


def test_unwatch_reports_a_pid_it_could_not_stop_and_keeps_going(machine, capsys) -> None:
    machine.add_poller(101, "gitlab-mr", "33248")
    machine.add_poller(102, "gitlab-mr", "33248")
    transport.record_pid("gitlab-mr", "33248", 101)
    machine.refuse[101] = PermissionError("Operation not permitted")
    assert dispatcher.cmd_unwatch(["gitlab-mr", "33248"]) == 1
    assert 102 not in machine.alive
    out = capsys.readouterr().out
    assert "101" in out and "not permitted" in out


def test_unwatch_never_signals_pid_1_or_itself(machine, capsys) -> None:
    machine.add_poller(1, "gitlab-mr", "33248")
    machine.add_poller(os.getpid(), "gitlab-mr", "33248")
    machine.add_poller(101, "gitlab-mr", "33248")
    dispatcher.cmd_unwatch(["gitlab-mr", "33248"])
    assert machine.killed_pids == {101}
    capsys.readouterr()


# ---------------------------------------------------------------------------
# the sequence actually observed in #511, start to finish
# ---------------------------------------------------------------------------

def test_the_observed_sequence(machine, capsys) -> None:
    """Two live pollers on one id -> both listed -> both stopped -> honest after."""
    machine.add_poller(92379, "gitlab-mr-feed", "author=@me,state=opened")
    machine.add_poller(92411, "gitlab-mr-feed", "author=@me,state=opened")
    transport.record_pid("gitlab-mr-feed", "author=@me,state=opened", 92379)

    assert dispatcher.cmd_list() == 0
    listed = capsys.readouterr().out
    assert "92379" in listed and "92411" in listed

    assert dispatcher.cmd_unwatch(["gitlab-mr-feed", "author=@me,state=opened"]) == 0
    stopped = capsys.readouterr().out
    assert "92379" in stopped and "92411" in stopped
    assert machine.alive == set()

    assert dispatcher.cmd_unwatch(["gitlab-mr-feed", "author=@me,state=opened"]) == 0
    again = capsys.readouterr().out.lower()
    assert "no active watcher" in again

    assert dispatcher.cmd_list() == 0
    assert "No active watchers." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# #484 / #490 must survive this
# ---------------------------------------------------------------------------

def test_a_second_watch_for_a_claimed_slot_still_refuses(monkeypatch, machine, capsys) -> None:
    spawned: list[tuple[str, str]] = []

    def _spawn(source, watcher_id, only):
        spawned.append((source, watcher_id))
        return machine.add_poller(101 + len(spawned), source, watcher_id, only)

    monkeypatch.setattr(dispatcher, "_spawn_poller", _spawn)
    assert dispatcher.cmd_watch(["gitlab-mr", "33248"]) == 0
    assert dispatcher.cmd_watch(["gitlab-mr", "33248"]) == 0
    assert len(spawned) == 1
    assert "not starting a second" in capsys.readouterr().out


def test_start_poller_still_claims_before_it_forks(monkeypatch, machine) -> None:
    """#484's ordering: losing the race must cost nothing to unwind."""
    seen: list[int] = []

    def _spawn(source, watcher_id, only):
        seen.append(transport.read_pid(source, watcher_id))
        return machine.add_poller(101, source, watcher_id, only)

    monkeypatch.setattr(dispatcher, "_spawn_poller", _spawn)
    dispatcher.start_poller("gitlab-mr", "33248", [])
    assert seen == [os.getpid()]
