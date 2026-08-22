"""#1881 — `watches` printed a fleet-wide absence while 564 pollers were live.

The machine sat at load average 409 with 564 orphaned `dispatcher.py poll`
processes across five slots, and `watches` said:

    No active watchers. None recorded as lost either.

That sentence is a claim about the fleet. It was printed on the strength of a
scan that **ran, succeeded, and saw all 564** — `scan_poller_pids` classified
every one of them as another channel's and dropped it, which #1514 decided
correctly for *rows and actions* and never revisited for the *render*. So the
board is not merely quiet about pollers it may not act on; it actively asserts
they do not exist. That is this repo's standing defect class — an absence
produced by the tool, read as an absence in the world — landing on the one
surface an operator consults before reaching for `pkill`.

**The reporter's suspected mechanism does not survive its own evidence, and
`test_the_reported_channel_token_is_this_state_dirs_own` is the arithmetic.**
The inference was that `SUPERTOOL_WATCH_NAME` rotates per session, so each new
session gets a fresh state directory, sees an empty slot, and spawns a duplicate.
But the channel token is `sha256(normpath(STATE_DIR))[:12]`, and STATE_DIR is
`naming.state_dir_for(name)` — so a rotating name produces a *different* token
every generation. All 564 pollers carried one token, `43b6d3f23b71`, and that is
exactly `channel_key("/tmp/supertool-watch-fdavid-dvsi-5535f2d5")`, the directory
the report names as current. One token means one state directory spawned all of
them. The cross-session accumulation story is refuted; what remains unexplained
is the half the report already flagged as unexplained (six same-minute zero-byte
`write_state` temporaries on one slot, against `claim_pidfile`'s `O_CREAT|O_EXCL`),
and that is filed separately rather than guessed at here.

So what is fixed here is the render, which is the part that is proven: three
states out of the scan instead of one, and a board that discloses the two it may
not act on rather than deleting them.

Nothing here spawns, signals or reaps a real process. The process table is a
list, liveness is a set.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import posixpath
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


dispatcher = _load("watch_dispatcher_1881", WATCH_DIR / "dispatcher.py")

#: The five slots the 564 pollers covered, from the report.
REPORTED_SLOTS = [
    ("gitlab-mr", "19509"),
    ("gitlab-mr", "33952"),
    ("gitlab-mr", "33992"),
    ("gitlab-mr-feed", "@me"),
    ("gl-runners", "fleet"),
]


class _Machine:
    """A process table and a liveness set. Signals nothing."""

    def __init__(self, base: Path, mine: Path, theirs: Path) -> None:
        self.base = str(base)
        self.mine = str(mine)
        self.theirs = str(theirs)
        self.rows: list[tuple[int, list[str]]] = []
        self.alive: set[int] = set()
        self.scan_breaks = False

    def argv_under(self, state_dir: str, source: str, watcher_id: str) -> list[str]:
        saved = transport.STATE_DIR
        transport.STATE_DIR = state_dir
        try:
            return transport.poller_argv(source, watcher_id, [])
        finally:
            transport.STATE_DIR = saved

    def add(self, state_dir: str, pid: int, source: str, watcher_id: str) -> int:
        self.rows.append((pid, self.argv_under(state_dir, source, watcher_id)))
        self.alive.add(pid)
        return pid

    def add_mine(self, pid: int, source: str, watcher_id: str) -> int:
        return self.add(self.mine, pid, source, watcher_id)

    def add_theirs(self, pid: int, source: str, watcher_id: str) -> int:
        return self.add(self.theirs, pid, source, watcher_id)

    def add_unlabelled(self, pid: int, source: str, watcher_id: str) -> int:
        """A poller predating the channel token: not ours, not theirs, unknowable."""
        self.rows.append((pid, [
            sys.executable, str(WATCH_DIR / "dispatcher.py"),
            transport.POLL_SUBOP, source, watcher_id,
        ]))
        self.alive.add(pid)
        return pid

    def add_reported_fleet(self, count: int = 564, first_pid: int = 1000) -> None:
        """The report's shape: `count` pollers on one other channel, five slots."""
        for n in range(count):
            source, watcher_id = REPORTED_SLOTS[n % len(REPORTED_SLOTS)]
            self.add_theirs(first_pid + n, source, watcher_id)

    def ps_rows(self):
        if self.scan_breaks:
            # What `_ps_rows` returns when `ps` could not be read at all. None
            # and [] are different answers; this fixture can produce both.
            return None
        return [(pid, argv) for pid, argv in self.rows if pid in self.alive]

    def pid_alive(self, pid: int) -> bool:
        return pid in self.alive


@pytest.fixture
def machine(tmp_path, monkeypatch) -> _Machine:
    base = tmp_path
    mine = base / "supertool-watch-oss-supertool"
    theirs = base / "supertool-watch-fdavid-dvsi-5535f2d5"
    mine.mkdir()
    theirs.mkdir()
    monkeypatch.setattr(transport, "STATE_DIR", str(mine))
    monkeypatch.setattr(dispatcher.transport, "STATE_DIR", str(mine))
    monkeypatch.setattr(naming, "BASE_DIR", str(base))
    m = _Machine(base, mine, theirs)
    monkeypatch.setattr(transport, "_ps_rows", m.ps_rows)
    monkeypatch.setattr(transport, "_pid_alive", m.pid_alive)
    monkeypatch.setattr(transport, "ps_scan_supported", lambda: True)
    # No channel disclosure banner in the way of the assertions below.
    monkeypatch.setattr(transport, "channel_disclosure", lambda: [])
    monkeypatch.setattr(dispatcher.transport, "channel_disclosure", lambda: [])
    return m


def _scanned(census: dict) -> dict:
    """Every count assertion in this file goes through here.

    A census whose scan did not run reports zero of everything, and zero is the
    shape of a clean answer. Asserting a count against an unscanned census is
    the silence-assertion trap this subsystem keeps falling into, so the guard
    fails loudly rather than letting the count read as evidence.
    """
    assert census["scan_ok"], (
        "the process scan did not run, so no count in this census is evidence "
        "of anything -- this assertion would have passed on a broken harness")
    return census


# ---------------------------------------------------------------------------
# the arithmetic that refutes the reported mechanism
# ---------------------------------------------------------------------------

#: The two state directories the report lists for the same project, and the one
#: token all 564 pollers carried.
REPORTED_DIR = "/tmp/supertool-watch-fdavid-dvsi-5535f2d5"
OTHER_DIR = "/tmp/supertool-watch-fdavid-dvsi-08e9bc4b"
REPORTED_TOKEN = "43b6d3f23b71"


def _posix_channel_key(state_dir: str) -> str:
    """`channel_key`'s arithmetic with POSIX path semantics pinned explicitly.

    `channel_key` normalises with `os.path.normpath`, which is `ntpath` on
    Windows: it turns `/tmp/x` into `\\tmp\\x`, a different string and therefore
    a different digest — `820a44249a9a` rather than `43b6d3f23b71`, measured
    rather than reasoned. The fact under test is about a macOS machine's
    directory, so the normalisation has to be the one *that* machine used.
    Reading it off the host would quietly turn this into a claim about the
    runner, and the assertion would fail on the Windows leg for a reason that
    has nothing to do with what it is asserting.
    """
    normalised = posixpath.normpath(state_dir)
    return hashlib.sha256(
        os.fsencode(normalised)).hexdigest()[:transport._CHANNEL_KEY_CHARS]


def test_the_reported_channel_token_is_this_state_dirs_own() -> None:
    """`chan=43b6d3f23b71` is the current directory's hash, not a stale one.

    If a rotating `SUPERTOOL_WATCH_NAME` had produced one poller per session,
    each generation would carry a different token, because the token is a hash
    of the state directory the name derives. One token across 564 pollers means
    one directory spawned every one of them.

    Platform-independent: the digest is computed here with POSIX semantics on
    every host, because the machine in the report was one.
    """
    assert _posix_channel_key(REPORTED_DIR) == REPORTED_TOKEN

    # must-fire, same fact: the *other* directory the report lists on the same
    # machine hashes elsewhere. Without this the assertion above would pass on a
    # helper that returned a constant.
    assert _posix_channel_key(OTHER_DIR) != REPORTED_TOKEN


@pytest.mark.skipif(
    os.path is not posixpath,
    reason="channel_key normalises with this host's os.path, and the fact under "
           "test is about a POSIX machine's directory. What goes untested here "
           "is only that channel_key and the POSIX arithmetic agree on THIS "
           "host; the arithmetic itself is asserted on every platform above.")
def test_channel_key_agrees_with_that_arithmetic_on_a_posix_host() -> None:
    """Ties the helper back to the production function it is standing in for.

    Without this the test above could drift into asserting a hash that
    `channel_key` no longer computes, and would keep passing.
    """
    assert transport.channel_key(REPORTED_DIR) == REPORTED_TOKEN
    assert transport.channel_key(OTHER_DIR) != REPORTED_TOKEN


# ---------------------------------------------------------------------------
# the census: three states out of one scan
# ---------------------------------------------------------------------------

def test_the_census_separates_mine_from_another_channels_from_unknowable(
        machine) -> None:
    machine.add_mine(101, "gitlab-mr", "33698")
    machine.add_theirs(202, "gitlab-mr", "19509")
    machine.add_unlabelled(303, "gl-runners", "fleet")
    census = _scanned(transport.poller_census())

    assert census["mine"] == {("gitlab-mr", "33698"): [101]}
    assert census["other"] == {
        transport.channel_key(machine.theirs): {("gitlab-mr", "19509"): [202]}}
    assert census["unknown"] == {("gl-runners", "fleet"): [303]}


def test_scan_poller_pids_still_answers_only_with_this_channels(machine) -> None:
    """#1514's contract. Every caller of it decides an *action*, and widening it
    would resurrect the cross-channel kill that issue was filed for."""
    machine.add_mine(101, "gitlab-mr", "33698")
    machine.add_theirs(202, "gitlab-mr", "19509")
    found, scan_ok = transport.scan_poller_pids()
    assert scan_ok
    assert found == {("gitlab-mr", "33698"): [101]}


def test_a_census_that_could_not_scan_says_so_rather_than_reporting_none(
        machine) -> None:
    """The third state. Zero elsewhere and *nobody looked* must not be one value."""
    machine.add_reported_fleet()
    machine.scan_breaks = True
    census = transport.poller_census()
    assert census["scan_ok"] is False
    assert census["mine"] == {}
    assert census["other"] == {}
    assert census["unknown"] == {}

    # must-fire, same fixture: the identical fleet with a working scan is seen.
    machine.scan_breaks = False
    assert _scanned(transport.poller_census())["other"] != {}


# ---------------------------------------------------------------------------
# the board — the render the issue was filed against
# ---------------------------------------------------------------------------

def _slot_rows(out: str, source: str, watcher_id: str) -> list[str]:
    """Board rows whose SOURCE and ID cells are this slot (the #1736 idiom)."""
    return [ln for ln in out.splitlines()
            if ln.split()[:2] == [source, watcher_id]]


def test_the_board_does_not_report_an_empty_fleet_while_564_pollers_run(
        machine, capsys) -> None:
    """The live render in #1881, at the size it was reported."""
    machine.add_reported_fleet()
    _scanned(transport.poller_census())

    assert dispatcher.cmd_list() == 0
    out = capsys.readouterr().out

    assert "No active watchers" not in out, out
    assert "564" in out, out

    # Disclosed, never promoted: no slot row, and no action offered against a
    # slot this board cannot reach.
    for source, watcher_id in REPORTED_SLOTS:
        assert _slot_rows(out, source, watcher_id) == [], out
    assert "no pidfile" not in out, out
    assert "unwatch:gitlab-mr:19509" not in out, out


def test_a_genuinely_empty_machine_still_says_so(machine, capsys) -> None:
    """The must-fire half of the assertion above.

    `"No active watchers" not in out` passes just as well on a board that
    printed nothing at all. This is the same board with an empty process table,
    and it must still produce the plain sentence.
    """
    assert _scanned(transport.poller_census())["other"] == {}
    assert dispatcher.cmd_list() == 0
    out = capsys.readouterr().out
    assert "No active watchers. None recorded as lost either." in out, out


def test_the_disclosure_survives_a_board_that_has_rows_of_its_own(
        machine, capsys) -> None:
    """564 pollers elsewhere matter exactly as much when this channel has two.

    A disclosure written only into the empty-board arm is one that vanishes the
    moment the operator has anything of their own, which is most of the time.
    """
    machine.add_mine(101, "gitlab-mr", "33698")
    machine.add_reported_fleet()
    assert dispatcher.cmd_list() == 0
    out = capsys.readouterr().out
    assert "564" in out, out
    assert len(_slot_rows(out, "gitlab-mr", "33698")) == 1, out


def test_pollers_with_no_channel_token_are_disclosed_as_unknowable(
        machine, capsys) -> None:
    """Not this channel's and not another's: the state nothing in an argv settles.

    Folding these into the other-channel count would be a claim the evidence
    does not support, and dropping them is how #511's population went invisible.
    """
    machine.add_unlabelled(303, "gitlab-mr", "19509")
    machine.add_unlabelled(304, "gl-runners", "fleet")
    assert dispatcher.cmd_list() == 0
    out = capsys.readouterr().out
    assert "No active watchers" not in out, out
    assert "channel cannot be told" in out, out
    assert _slot_rows(out, "gitlab-mr", "19509") == [], out


def test_the_disclosure_resolves_a_channel_token_to_its_state_directory(
        machine, capsys) -> None:
    """The operator's route out, and the reason this is worth printing.

    A bare `chan=43b6d3f23b71` is not actionable: the token is a hash and cannot
    be reversed. Sibling directories under BASE_DIR can be hashed forward, which
    turns the token into the directory whose own `watches` *can* act on it.
    """
    machine.add_reported_fleet()
    assert dispatcher.cmd_list() == 0
    out = capsys.readouterr().out
    assert "supertool-watch-fdavid-dvsi-5535f2d5" in out, out


def test_an_unresolvable_channel_token_is_named_rather_than_dropped(
        machine, capsys, monkeypatch) -> None:
    """A poller whose state directory is gone is still 564 processes.

    The forward hash finds nothing, and the honest answer is the token with a
    note that no directory here matches it -- not a shorter disclosure.
    """
    gone = os.path.join(machine.base, "supertool-watch-deleted-since")
    machine.add(gone, 900, "gitlab-mr", "19509")
    assert dispatcher.cmd_list() == 0
    out = capsys.readouterr().out
    assert transport.channel_key(gone) in out, out
    assert "no state directory" in out, out


def test_a_board_whose_scan_failed_does_not_print_a_zero_disclosure(
        machine, capsys) -> None:
    """The render half of the third state.

    `0 pollers on another channel` read off a scan that never ran is the exact
    substitution this issue is about, one layer in.
    """
    machine.add_reported_fleet()
    machine.scan_breaks = True
    assert dispatcher.cmd_list() == 0
    out = capsys.readouterr().out
    assert "No active watchers. None recorded as lost either." not in out, out
    assert "0 poller" not in out, out
    assert "could not be read this time" in out, out


# ---------------------------------------------------------------------------
# the sibling-directory scan is itself three-state
# ---------------------------------------------------------------------------

def test_channel_dirs_maps_every_sibling_state_directory(machine) -> None:
    mapping, state, _why = transport.channel_dirs()
    assert state == naming.STATE_DIR_OK
    assert mapping[transport.channel_key(machine.mine)] == machine.mine
    assert mapping[transport.channel_key(machine.theirs)] == machine.theirs


def test_channel_dirs_declines_when_the_base_directory_cannot_be_listed(
        machine, monkeypatch) -> None:
    """An unlistable BASE_DIR must not render as "no sibling channels exist"."""
    monkeypatch.setattr(
        naming, "BASE_DIR", os.path.join(machine.base, "no-such-dir"))
    mapping, state, _why = transport.channel_dirs()
    assert mapping == {}
    assert state != naming.STATE_DIR_OK

    # must-fire: the real base directory answers, so the assertion above is
    # about the missing directory and not about a helper that never works.
    monkeypatch.setattr(naming, "BASE_DIR", machine.base)
    assert transport.channel_dirs()[1] == naming.STATE_DIR_OK


# ---------------------------------------------------------------------------
# cross-platform
# ---------------------------------------------------------------------------

def test_a_machine_that_can_never_scan_gets_the_permanent_disclosure(
        machine, capsys, monkeypatch) -> None:
    """Windows and any other machine whose `ps` cannot answer the invocation.

    Reasoned, not observed on Windows: the platform arm is `ps_scan_supported`,
    which is already probed rather than assumed (#786). What this pins is that
    the *census* lands in its third state there rather than reporting an empty
    fleet, so the Windows leg asserts something rather than passing vacuously.
    """
    machine.add_reported_fleet()
    machine.scan_breaks = True
    monkeypatch.setattr(transport, "ps_scan_supported", lambda: False)
    census = transport.poller_census()
    assert census["scan_ok"] is False
    assert dispatcher.cmd_list() == 0
    out = capsys.readouterr().out
    assert "No active watchers. None recorded as lost either." not in out, out
    assert "process scan cannot answer" in out, out


# ---------------------------------------------------------------------------
# the channel token is untrusted text, not the tool's own -- #1925
# ---------------------------------------------------------------------------

def test_a_csi_sequence_in_the_channel_token_is_neutralised(machine, capsys) -> None:
    """A local process can name its own argv, so `chan=` is as untrusted as any
    field #1197 already flattens -- and `_ps_rows` splits on whitespace, which
    blocks a newline but lets a CSI sequence (no whitespace in it) straight
    through. `ESC[1G` moves the cursor to column 1, `ESC[2K` erases the line:
    together they let another process rewrite this exact disclosure from
    column 0, on the surface whose only job is to say a poller survived.
    """
    hostile = "aa\x1b[1G\x1b[2Kpwned"
    pid = 909
    machine.rows.append((pid, [
        sys.executable, str(WATCH_DIR / "dispatcher.py"),
        transport.POLL_SUBOP, "gitlab-mr", "19509",
        transport.CHANNEL_PREFIX + hostile,
    ]))
    machine.alive.add(pid)
    assert dispatcher.cmd_list() == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out, out  # the raw escape must never reach the stream
    assert "␛" in out, out  # flat()'s visible glyph for ESC (#1557)


def test_an_ordinary_hex_channel_token_renders_unchanged(machine, capsys) -> None:
    """The must-fire control half: neutralising must not turn into mangling.

    Without this, a fix that ran every channel token through something coarser
    than `flat()` -- hashing it again, or replacing it outright -- would still
    pass the test above while breaking the token every real poller sends.
    """
    machine.add_reported_fleet()
    assert dispatcher.cmd_list() == 0
    out = capsys.readouterr().out
    assert transport.channel_key(machine.theirs) in out, out


# --- documentation ---------------------------------------------------------

def test_the_change_is_findable():
    assert_change_is_findable(1881)
