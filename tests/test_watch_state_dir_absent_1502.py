"""A read path may not crash on a state directory no spawn has created (#1502).

`SUPERTOOL_WATCH_NAME` derives `/tmp/supertool-watch-<name>` and only the **spawn**
path creates it (`naming.ensure_state_dir`, deliberately, because a
`SUPERTOOL_WATCH_STATE_DIR` the operator supplied is unanswerable rather than
manufacturable — #693). The default state directory is `/tmp` itself, so
`os.listdir(STATE_DIR)` in `list_active_pids` was unreachable-by-luck on the
default and fired on the **first read after naming a channel**:

    $ supertool 'channel:health' 'watches'
    FileNotFoundError: [Errno 2] No such file or directory: '/tmp/supertool-watch-oss'
    [batch] 2 ops ran - all 2 refused.

Two things pinned here, and the second is the reason this is not a `mkdir`:

* **No read path creates the directory.** Manufacturing it would resurrect #693
  for an operator-supplied path and make a read have side effects.
* **Absent and unreadable are different answers.** An absent directory is a
  knowable state - zero watchers, nothing has ever spawned on this channel. A
  directory that exists and could not be listed is *unknown*, and a board that
  renders it as `No active watchers` is the absence-read-as-presence defect.
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WATCH_DIR = REPO / "presets" / "watch"
for _dir in (str(WATCH_DIR), str(REPO / "presets"), str(REPO / "tests")):
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

from _changelog_findable import assert_change_is_findable  # noqa: E402


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dispatcher = _module("watch_dispatcher_1502", WATCH_DIR / "dispatcher.py")
transport = dispatcher.transport


def _quiet_fleet(monkeypatch) -> None:
    """No poller on the developer's machine may wander onto the board."""
    monkeypatch.setattr(transport, "scan_poller_pids", lambda: ({}, True))
    monkeypatch.setattr(transport, "ps_scan_supported", lambda: True)


def _absent(tmp_path: Path) -> Path:
    """A derived state directory nothing has spawned into yet."""
    return tmp_path / "supertool-watch-oss"


# --- the enumeration itself -------------------------------------------------

def test_listing_pids_in_an_absent_state_directory_is_zero_watchers(
        monkeypatch, tmp_path) -> None:
    absent = _absent(tmp_path)
    monkeypatch.setattr(transport, "STATE_DIR", str(absent))
    assert transport.list_active_pids() == []


def test_list_watchers_answers_over_an_absent_state_directory(
        monkeypatch, tmp_path) -> None:
    _quiet_fleet(monkeypatch)
    monkeypatch.setattr(transport, "STATE_DIR", str(_absent(tmp_path)))
    rows, _scan_ok = transport.list_watchers()
    assert rows == []


def test_no_read_path_creates_the_state_directory(monkeypatch, tmp_path) -> None:
    """The whole reason this is not a `mkdir` (#693). A read that creates state
    is a worse trade than a read that declines to answer."""
    _quiet_fleet(monkeypatch)
    absent = _absent(tmp_path)
    monkeypatch.setattr(transport, "STATE_DIR", str(absent))
    transport.list_active_pids()
    transport.list_watchers()
    dispatcher.cmd_list()
    assert not absent.exists()


# --- absent is not unreadable ----------------------------------------------

def test_the_status_of_an_absent_directory_is_absent(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(transport, "STATE_DIR", str(_absent(tmp_path)))
    state, why = transport.state_dir_status()
    assert state == transport.STATE_DIR_ABSENT
    assert why == ""


def test_the_status_of_a_readable_directory_is_ok(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path))
    assert transport.state_dir_status() == (transport.STATE_DIR_OK, "")


def _listdir_raises(monkeypatch, err: OSError) -> None:
    """Make the enumeration fail with a chosen error, on every platform.

    Deliberately injected rather than provoked. The obvious way to produce "it
    exists and cannot be listed" is a regular file at the name, but *which*
    `OSError` that raises is the platform's choice - POSIX says
    `NotADirectoryError`, Windows has returned `WinError 267` for the file and
    `WinError 3` for a path through it, and the second maps to
    `FileNotFoundError`, which is the other arm. That is #620/#627's shape
    exactly: a handler keyed to the POSIX exception never fires on Windows and
    the coverage is reported anyway. So the classification is asserted over an
    error this test chose, and the provoked case below asserts only the part
    that is true whichever error arrives - that nothing escapes.
    """
    def boom(_path):
        raise err
    monkeypatch.setattr(transport.os, "listdir", boom)


def test_a_directory_that_cannot_be_enumerated_is_unknown_not_absent(
        monkeypatch, tmp_path) -> None:
    """The case that matters: it is there, and this uid cannot read it."""
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path))
    _listdir_raises(monkeypatch, PermissionError(13, "Permission denied"))
    state, why = transport.state_dir_status()
    assert state == transport.STATE_DIR_UNREADABLE
    assert why, "an unreadable directory must say why"
    assert str(tmp_path) in why
    assert "PermissionError" in why


def test_only_a_missing_directory_is_absent(monkeypatch, tmp_path) -> None:
    """`FileNotFoundError` is the one error that means zero watchers. Every
    other `OSError` leaves the population unknown, and the two must not swap:
    `NotADirectoryError` is not a `FileNotFoundError` on any platform."""
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path))
    _listdir_raises(monkeypatch, FileNotFoundError(2, "No such file"))
    assert transport.state_dir_status()[0] == transport.STATE_DIR_ABSENT
    _listdir_raises(monkeypatch, NotADirectoryError(20, "Not a directory"))
    assert transport.state_dir_status()[0] == transport.STATE_DIR_UNREADABLE


def test_an_unenumerable_directory_still_does_not_raise(
        monkeypatch, tmp_path) -> None:
    """The provoked case, asserted only on what holds whichever error the
    platform picks for a regular file at the name."""
    _quiet_fleet(monkeypatch)
    not_a_dir = tmp_path / "supertool-watch-oss"
    not_a_dir.write_text("", encoding="utf-8")
    monkeypatch.setattr(transport, "STATE_DIR", str(not_a_dir))
    assert transport.list_active_pids() == []
    assert transport.list_watchers()[0] == []
    assert transport.state_dir_status()[0] in (
        transport.STATE_DIR_UNREADABLE, transport.STATE_DIR_ABSENT)


# --- the render -------------------------------------------------------------

def test_watches_says_nothing_has_spawned_on_this_channel_yet(
        monkeypatch, tmp_path, capsys) -> None:
    _quiet_fleet(monkeypatch)
    absent = _absent(tmp_path)
    monkeypatch.setattr(transport, "STATE_DIR", str(absent))
    assert dispatcher.cmd_list() == 0
    out = capsys.readouterr().out
    assert str(absent) in out, out
    assert "does not exist yet" in out, out
    assert "spawn" in out, out


def test_watches_does_not_render_an_unlistable_directory_as_no_watchers(
        monkeypatch, tmp_path, capsys) -> None:
    """`No active watchers. None recorded as lost either.` is a claim about the
    fleet. It may not be printed on the strength of a listing that never ran."""
    _quiet_fleet(monkeypatch)
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path))
    _listdir_raises(monkeypatch, PermissionError(13, "Permission denied"))
    assert dispatcher.cmd_list() == 0
    out = capsys.readouterr().out
    assert "None recorded as lost either" not in out, out
    assert "could not be listed" in out, out
    assert "not evidence of absence" in out, out


def test_an_empty_but_present_state_directory_still_reads_as_no_watchers(
        monkeypatch, tmp_path, capsys) -> None:
    """The pre-existing answer must not be taken away by the new arms: a
    directory that exists and holds nothing IS zero watchers."""
    _quiet_fleet(monkeypatch)
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path))
    assert dispatcher.cmd_list() == 0
    out = capsys.readouterr().out
    assert "No active watchers. None recorded as lost either." in out, out
    assert "does not exist yet" not in out, out


def test_an_absent_directory_still_discloses_an_unavailable_process_scan(
        monkeypatch, tmp_path, capsys) -> None:
    """Two independent gaps. The new arm must not swallow the older disclosure -
    an untracked poller cannot be ruled out on either."""
    monkeypatch.setattr(transport, "scan_poller_pids", lambda: ({}, False))
    monkeypatch.setattr(transport, "ps_scan_supported", lambda: False)
    monkeypatch.setattr(transport, "STATE_DIR", str(_absent(tmp_path)))
    assert dispatcher.cmd_list() == 0
    out = capsys.readouterr().out
    assert "does not exist yet" in out, out
    assert "process scan" in out, out


# --- the sweep --------------------------------------------------------------

def test_every_state_dir_enumeration_in_transport_is_guarded() -> None:
    """`radar:--state` survived this by never enumerating, which is luck rather
    than a guard. The named class is an unguarded enumeration, so the file is
    swept for the class rather than for the one instance that was reported."""
    text = (WATCH_DIR / "transport.py").read_text(encoding="utf-8")
    tree = ast.parse(text)
    guard = next(node for node in ast.walk(tree)
                 if isinstance(node, ast.FunctionDef)
                 and node.name == "_state_dir_names")
    span = range(guard.lineno, (guard.end_lineno or guard.lineno) + 1)
    outside = [f"transport.py:{n}: {line.strip()}"
               for n, line in enumerate(text.splitlines(), 1)
               if "os.listdir(" in line and n not in span]
    assert outside == [], (
        "every enumeration of the state directory must go through "
        "`_state_dir_names`, so a directory no spawn has created cannot raise "
        "out of any reader: " + str(outside))


def test_the_change_is_findable() -> None:
    assert_change_is_findable(1502)
