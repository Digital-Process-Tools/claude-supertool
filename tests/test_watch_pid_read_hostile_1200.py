"""`transport.read_pid` reads an unreadable pid file as an empty slot (#1200).

Fifth and sixth call sites of the pair fixed at `channel.read_health`
(#1184/#1187), `channel.stranded_watchers` (#1191) and `transport.read_state`
(#1197). Same predictable `/tmp` name, same missing `O_NOFOLLOW` — and a
different blast radius, which is why it is its own issue:

* **`read_pid` returned `0` for "no file" and for "the file could not be
  read".** `0` is not neutral: it is the sentinel meaning *the slot is free*.
  `claim_pidfile` reads it, concludes nobody owns the slot, **unlinks the
  file** and takes the claim — so a symlink planted at the pid path both
  destroys the real poller's claim and starts a second poller on the same
  filter. That is the duplicate-watcher condition of 2026-08-01, where a
  genuine `pipeline_failed` went unannounced for 23 minutes under the flood.
* **`list_active_pids` unlinked on a read it could not perform.** An
  unreadable file is not evidence that a slot is stale, and the process that
  owned it cannot get its claim back.

**Absent, unreadable and unparseable are three answers, and only two of them
are the same.** `read_pid_checked` returns `None` when the read itself failed,
and `0` both when there is honestly no file and when a file exists whose
content is not a PID. That second collapse is deliberate and is pinned below:
a pidfile nobody can attribute to a process must stay reclaimable, or a poller
that died mid-write wedges its slot shut forever — which `claim_pidfile`'s own
docstring says is worse than a duplicate.

Every test here builds the hostile file itself; none passes against code that
does nothing.

**Platforms.** The absent and unparseable tests write ordinary files and run
everywhere. The symlink tests need `os.symlink` *and* a non-zero
`transport._NOFOLLOW`, and the descriptor test needs POSIX lowest-free-fd
allocation. All are measured capabilities and skip rather than passing
vacuously on Windows.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from _symlink import require_symlink

REPO = Path(__file__).resolve().parents[1]
for _dir in (str(REPO / "presets" / "watch"), str(REPO / "presets"), str(REPO / "tests")):
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

import dispatcher  # noqa: E402
import transport  # noqa: E402
from _changelog_findable import assert_change_is_findable  # noqa: E402

SOURCE = "gh"
WATCHER = "9"

#: The PID recorded in the file a hostile symlink points at. Nothing that
#: follows the link may report it, and nothing may print it.
ELSEWHERE_PID = 424242

#: `_NOFOLLOW` is `getattr(os, "O_NOFOLLOW", 0)`, so on a platform without it
#: the flag is a no-op and the symlink is followed. Refusing to run there beats
#: asserting something the platform cannot deliver.
needs_symlink = pytest.mark.skipif(
    not hasattr(os, "symlink") or not getattr(transport, "_NOFOLLOW", 0),
    reason="needs os.symlink and a real O_NOFOLLOW",
)


@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    """A state directory of our own, and no process scan.

    The scan is stubbed empty because this machine has real pollers in the
    real `/tmp`: the scan reads `ps`, not `STATE_DIR`. `poller_census` and not
    `scan_poller_pids` since #1881, for the reason given in that issue's own
    test file — the board renders three buckets and the narrow stub covers one.
    """
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(transport, "poller_census",
                        lambda: transport.empty_census(True))
    return tmp_path


def _pid_file(tmp_path: Path, source: str = SOURCE, watcher_id: str = WATCHER) -> Path:
    return tmp_path / f"supertool-watch-{source}__{watcher_id}.pid"


def _hostile_symlink(tmp_path: Path) -> Path:
    """A symlink where the pid file goes, pointing at a file we also own.

    The target exists and holds a plausible PID, so following the link
    succeeds — which is the whole failure: the read works, and answers about
    a file this slot never wrote.
    """
    require_symlink()
    target = tmp_path / "elsewhere.pid"
    target.write_text(f"{ELSEWHERE_PID}\n", encoding="utf-8")
    path = _pid_file(tmp_path)
    os.symlink(target, path)
    return path


# --- the read itself: three states -----------------------------------------

def test_an_absent_pid_file_is_not_a_refusal(state_dir):
    """The overwhelmingly common answer, and the one three states must not
    complicate: this slot has published nothing."""
    assert transport.read_pid_checked(SOURCE, WATCHER) == (0, "")


def test_a_recorded_pid_is_returned_with_no_refusal(state_dir):
    _pid_file(state_dir).write_text("31337\n", encoding="utf-8")
    assert transport.read_pid_checked(SOURCE, WATCHER) == (31337, "")


@needs_symlink
def test_a_symlinked_pid_file_is_a_refusal_and_not_an_empty_slot(state_dir):
    """`0` means the slot is free. An unread file says nothing about that."""
    _hostile_symlink(state_dir)
    pid, refusal = transport.read_pid_checked(SOURCE, WATCHER)
    assert pid is None, (pid, refusal)
    assert "symlink" in refusal, refusal
    assert str(ELSEWHERE_PID) not in refusal, refusal


def test_a_pid_file_whose_content_is_not_a_pid_stays_reclaimable(state_dir):
    """Deliberately `0`, not `None` — see the module docstring. A poller that
    died mid-write must not wedge its slot shut forever."""
    _pid_file(state_dir).write_text("not-a-pid\n", encoding="utf-8")
    pid, refusal = transport.read_pid_checked(SOURCE, WATCHER)
    assert pid == 0
    assert refusal, "an unparseable file is still worth saying out loud"


@pytest.mark.skipif(os.name == "nt", reason="needs POSIX lowest-free-fd allocation")
def test_the_descriptor_is_not_leaked_when_the_path_is_a_directory(state_dir):
    """`O_NOFOLLOW` refuses a symlink and does *not* refuse a directory:
    `os.open` succeeds and `os.fdopen` raises without taking the descriptor.
    One leak per call, and `list_active_pids` calls this once per row."""
    _pid_file(state_dir).mkdir()
    before = os.open(os.devnull, os.O_RDONLY)
    os.close(before)
    for _ in range(12):
        pid, refusal = transport.read_pid_checked(SOURCE, WATCHER)
        assert pid is None, (pid, refusal)
    after = os.open(os.devnull, os.O_RDONLY)
    os.close(after)
    assert after == before, "a descriptor was leaked per call"


# --- claim_pidfile: the decision that spawns a poller ----------------------

@needs_symlink
def test_claiming_a_slot_whose_pid_file_cannot_be_read_is_refused(state_dir):
    """The whole issue. On master this returned `0` — *you own the slot now* —
    and a second poller was spawned onto a filter already covered."""
    _hostile_symlink(state_dir)
    assert transport.claim_pidfile(SOURCE, WATCHER) == transport.CLAIM_UNKNOWN


@needs_symlink
def test_claiming_does_not_destroy_a_claim_it_could_not_read(state_dir):
    """Unlinking another process's pid file is not recoverable by its owner.

    `islink`, not `lexists`: master unlinks the symlink and then creates its
    own pid file at the same name, so `lexists` is true on the broken code and
    this test would assert nothing.
    """
    path = _hostile_symlink(state_dir)
    transport.claim_pidfile(SOURCE, WATCHER)
    assert os.path.islink(path), "the unread pid file was replaced"


def test_claiming_still_reclaims_a_pid_file_that_names_no_process(state_dir):
    """The pin on the deliberate half: garbage content stays reclaimable, so a
    poller that crashed mid-write cannot wedge its slot shut."""
    _pid_file(state_dir).write_text("not-a-pid\n", encoding="utf-8")
    assert transport.claim_pidfile(SOURCE, WATCHER) == 0
    assert _pid_file(state_dir).read_text(encoding="utf-8").strip() == str(os.getpid())


# --- release_pidfile: the other way to destroy a claim ---------------------

@needs_symlink
def test_releasing_a_slot_does_not_unlink_when_ownership_cannot_be_confirmed(state_dir):
    """`release_pidfile(..., pid)` exists to avoid unlinking a successor's
    claim. A read that failed is not a confirmation that we still own it."""
    path = _hostile_symlink(state_dir)
    transport.release_pidfile(SOURCE, WATCHER, os.getpid())
    assert os.path.lexists(path)


# --- list_active_pids: the failure path that deletes -----------------------

@needs_symlink
def test_the_board_scan_does_not_unlink_a_pid_file_it_could_not_read(state_dir):
    """`transport.py:636` on master. An unreadable file is not evidence that
    the slot is stale."""
    path = _hostile_symlink(state_dir)
    transport.list_active_pids()
    assert os.path.lexists(path), "the unread pid file was deleted"


@needs_symlink
def test_the_board_scan_does_not_report_a_slot_it_could_not_read_as_active(state_dir):
    """Not-unlinking must not become claiming-it-is-live: the row would carry
    a PID read out of a file this slot never wrote."""
    _hostile_symlink(state_dir)
    rows = transport.list_active_pids()
    assert [r for r in rows if r["pid"] == ELSEWHERE_PID] == [], rows


def test_the_board_scan_still_prunes_a_pid_file_that_names_no_process(state_dir):
    """The pin: content that is not a PID stays prunable."""
    path = _pid_file(state_dir)
    path.write_text("not-a-pid\n", encoding="utf-8")
    transport.list_active_pids()
    assert not path.exists()


# --- watcher_pids: the report that has to say it did not know --------------

@needs_symlink
def test_watcher_pids_names_the_unread_pid_file_rather_than_reporting_none(state_dir):
    """`tracked: 0` is documented as *a slot with nothing in it*. Rendering an
    unread file as that is the absence-read-as-presence shape this repo keeps
    paying for."""
    _hostile_symlink(state_dir)
    info = transport.watcher_pids(SOURCE, WATCHER)
    assert info["tracked"] == 0
    assert info["tracked_refusal"], info


def test_watcher_pids_carries_no_refusal_when_the_slot_is_honestly_empty(state_dir):
    assert transport.watcher_pids(SOURCE, WATCHER)["tracked_refusal"] == ""


# --- the render ------------------------------------------------------------

@needs_symlink
def test_unwatch_says_the_pid_file_was_unreadable_instead_of_no_pid_file(state_dir, capsys):
    """A missing pid file and a pid file that would not be followed send an
    operator to two different places."""
    path = _hostile_symlink(state_dir)
    dispatcher.cmd_unwatch([SOURCE, WATCHER])
    out = capsys.readouterr().out
    assert "No PID file" not in out, out
    assert "could not be read" in out or "symlink" in out, out
    # The message tells the operator to inspect the path. `release_pidfile` with
    # no `pid` unlinks unconditionally, so without this assertion the message and
    # the behaviour disagreed and every test here still passed.
    assert os.path.islink(path), "unwatch deleted the path it told us to inspect"


@needs_symlink
def test_unwatch_leaves_an_unreadable_pid_file_alone_after_stopping_pollers(
        state_dir, monkeypatch, capsys):
    """The other `release_pidfile` call in `cmd_unwatch`, on the arm that did
    stop something. Stopping a poller says nothing about who wrote this name."""
    path = _hostile_symlink(state_dir)
    stopped: list[int] = []
    # `poller_census`, not `scan_poller_pids`: #1893 made `cmd_unwatch` read
    # the census directly (for the foreign-poller disclosure) and thread its
    # `mine` bucket into `watcher_pids` rather than let that call re-derive it
    # through `scan_poller_pids`. The `state_dir` fixture already stubs
    # `poller_census` to an empty one; this overrides it with the one PID this
    # test is about.
    monkeypatch.setattr(
        transport, "poller_census",
        lambda: {"mine": {(SOURCE, WATCHER): [4242]}, "other": {},
                 "unknown": {}, "scan_ok": True})
    monkeypatch.setattr(transport, "_pid_alive", lambda pid: pid == 4242)
    monkeypatch.setattr(dispatcher, "_stop_pid", lambda pid: stopped.append(pid) or "")
    dispatcher.cmd_unwatch([SOURCE, WATCHER])
    assert stopped == [4242]
    assert os.path.islink(path), "unwatch deleted a pid file it could not read"
    assert "left in place" in capsys.readouterr().out


@needs_symlink
def test_the_watches_board_still_shows_a_slot_whose_pid_file_is_unreadable(
        state_dir, monkeypatch, capsys):
    """Declining to prune must not make the slot invisible. The process scan
    is the surface that keeps it on the board, as an orphan row."""
    _hostile_symlink(state_dir)
    monkeypatch.setattr(
        transport, "poller_census",
        lambda: dict(transport.empty_census(True),
                     mine={(SOURCE, WATCHER): [os.getpid()]}))
    assert dispatcher.cmd_list() == 0
    assert WATCHER in capsys.readouterr().out


# --- documentation ---------------------------------------------------------

def test_the_change_is_findable():
    assert_change_is_findable(1200)
