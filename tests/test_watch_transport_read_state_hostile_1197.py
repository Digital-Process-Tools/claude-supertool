"""`transport.read_state` reads somebody else's file too (#1197).

Third instance of one defect pair, on the same files `channel.read_health`
(#1184/#1187, `db4e280`) and `channel.stranded_watchers` (#1191) had just been
fixed on. `read_state` opened a predictable name in a world-writable directory
with a plain `open()`, and caught `(OSError, json.JSONDecodeError)`.

What that measured on `923f7bc`, not what it looked like:

* **Two bytes of invalid UTF-8 take down five entry points.** `UnicodeDecodeError`
  is a `ValueError`, so it is in neither arm. `read_state`, `deaths`,
  `list_active_pids`, `list_watchers` and `dispatcher.cmd_list` all raised, and
  the `watches` op exited on a traceback out of `json/__init__.py`. It reaches
  the poll loop too (`dispatcher.py:617`), where it is not inside the
  never-crash `try` — so one file kills the watcher as well as the board.
* **A state file forges whole rows of the board.** `watches` prints a
  fixed-width table and interpolated `last_event` into it raw. A `last_event`
  carrying newlines put a complete, plausible `gitlab-mr 19509 ... all green`
  row on the board, for an MR that has no watcher.
* **A symlink at the name got any same-uid JSON file parsed**, exactly #1184.

**Where the flattening went, and why it is not in `read_state`.** `read_state`
is the read half of six read-modify-write cycles — `emit_event`,
`record_death`, `clear_deaths`, and `dispatcher.py:617/645/664` all read the
dict, mutate it and write it back. Flattening at the read would rewrite the
flattened form to disk on the next tick, including `source_state`, which is a
poller's private resume cursor and not report text. So the guards go at the
read and the flattening goes at the two renders, and
`test_read_state_stays_lossless_because_its_result_is_written_back` pins the
half that a later "helpful" change would otherwise undo.

Every test here constructs the hostile file itself; none passes against code
that does nothing.

**Platforms.** The render and decode tests write ordinary files and run
everywhere, Windows included. The symlink tests need `os.symlink` *and*
`os.O_NOFOLLOW`, and the descriptor test needs POSIX lowest-free-fd
allocation. All are measured capabilities and skip rather than passing
vacuously.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for _dir in (str(REPO / "presets" / "watch"), str(REPO / "presets"), str(REPO / "tests")):
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

import _symlink  # noqa: E402
import _untrusted  # noqa: E402
import transport  # noqa: E402
from _changelog_findable import assert_change_is_findable  # noqa: E402
# `dispatcher` is a contested basename -- `presets/worktree/dispatcher.py`
# claims it too (#726, #532) -- loaded by absolute path instead of a bare
# `import dispatcher`, which xdist's work split would resolve arbitrarily.
from _preset_loader import load_preset_module  # noqa: E402

dispatcher = load_preset_module("watch", "dispatcher", prefix="watch_1197_")

#: A complete row of the `watches` table, in the table's own column widths. Not
#: arbitrary text: it names a real-looking MR as watched and green, which is the
#: claim the board exists to make and the one nothing else on it contradicts.
FORGED_ROW = "gitlab-mr       19509  32471  2026-08-07T16:53:27Z  all green"

#: ESC-[-2-K then ESC-[-1-A: erase the line above and put the cursor on it —
#: #851's sequence, which removes a line the tool wrote rather than adding one.
HOSTILE_ERASE = "ok\x1b[2K\x1b[1Aforged"


@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    """A state directory of our own, and no process scan.

    The scan is stubbed to empty because this machine has real pollers in the
    real `/tmp`: the scan reads `ps`, not `STATE_DIR`, so without this the board
    under test carries rows these tests did not write.

    `poller_census` rather than `scan_poller_pids` since #1881: the board
    renders all three of the scan's buckets and that function is only one of
    them, so stubbing it alone left the other two reading the real process
    table and the isolation this fixture promises was not real.
    """
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(transport, "poller_census",
                        lambda: transport.empty_census(True))
    return tmp_path


def _slot(tmp_path: Path, source: str = "gh", watcher_id: str = "9", **state) -> Path:
    """A live watcher: a pid file naming this process, and a state file."""
    (tmp_path / f"supertool-watch-{source}__{watcher_id}.pid").write_text(
        str(os.getpid()), encoding="utf-8")
    target = tmp_path / f"supertool-watch-{source}__{watcher_id}.state.json"
    target.write_text(json.dumps(state), encoding="utf-8")
    return target


def _board(capsys) -> str:
    """What the `watches` op prints."""
    assert dispatcher.cmd_list() == 0
    return capsys.readouterr().out


# --- the render: a state file must not write lines of the board -------------

def test_a_forged_row_in_last_event_cannot_stand_alone_on_the_board(state_dir, capsys):
    """A line of the board is a line the tool wrote.

    Both newlines are load-bearing and this test is vacuous without either.
    `last_event` is the final column, so text after the forgery is needed to
    keep the trailing one from being eaten by `rstrip`, and text before it is
    needed to get the forgery off the row's own physical line. Measured
    against 923f7bc: with the pair, the forged row stands alone in the output.
    """
    _slot(state_dir, last_event={"event": "ok\n" + FORGED_ROW + "\nzz", "ts": "t"})
    board = _board(capsys)
    assert FORGED_ROW not in board.split("\n"), board


def test_the_forged_row_is_disclosed_rather_than_dropped(state_dir, capsys):
    """Suppressing it turns "this file was hostile" into "this file was
    empty", which is this repo's defect wearing a fix's clothing.

    Asserted on the whole flattened value rather than on `"all green" in
    board`: the review of this commit measured that the weaker form passes
    against the pre-fix code too, because raw-and-forged and flat-and-disclosed
    both contain those two words. This form fails against 29ee138.
    """
    hostile = "ok\n" + FORGED_ROW + "\nzz"
    _slot(state_dir, last_event={"event": hostile, "ts": "t"})
    assert _untrusted.flat(hostile) in _board(capsys)


def test_an_escape_sequence_in_last_event_never_reaches_the_terminal(state_dir, capsys):
    """Worse than a forged line: it removes a line the op wrote (#851)."""
    _slot(state_dir, last_event={"event": HOSTILE_ERASE, "ts": "t"})
    board = _board(capsys)
    assert "\x1b" not in board, board
    assert ("␛" in board) or ("[U+001B]" in board), board


def test_the_board_render_is_flat_itself_and_not_a_second_scheme(state_dir, capsys):
    """Equality with `flat`, not a grep for its name: a local reimplementation
    would have to be widened again the next time `_untrusted` is (#851, #886).
    A tab is what a hand-rolled splitlines-and-join gets wrong, and a tab in a
    fixed-width column is its own misrender besides."""
    _slot(state_dir, last_event={"event": "a\tb", "ts": "t"})
    board = _board(capsys)
    assert _untrusted.flat("a\tb") in board, board
    assert "a\tb" not in board, board


def test_the_watcher_name_out_of_the_filename_is_flattened_too(state_dir, capsys):
    """`source` and the id are parsed out of a *filename*, and the directory
    accepts whatever a co-tenant creates. A POSIX filename carries any byte but
    `/` and NUL, newline included — so the board's first two columns are as
    much somebody else's words as its last one."""
    try:
        _slot(state_dir, source="gh\n" + FORGED_ROW + "\nzz",
              last_event={"event": "ok", "ts": "t"})
    except (OSError, ValueError):
        pytest.skip("this filesystem does not accept a newline in a filename")
    board = _board(capsys)
    assert FORGED_ROW not in board.split("\n"), board


def test_the_feed_error_message_is_flattened_on_the_gl_mrs_board(state_dir):
    """The second render of a `read_state` string, and the reason "flatten at
    each render" needs every render named: `gl-mrs` prints the feed poller's
    `last_error.message`, which the dispatcher writes from a poller exception
    into the same world-writable file."""
    from tiers import gl_mrs  # noqa: PLC0415 — import cost, and only this test needs it

    _slot(state_dir, source=gl_mrs.FEED_SOURCE, watcher_id=gl_mrs.FEED_SCOPE,
          last_error={"message": "boom\n" + FORGED_ROW + "\nzz"})
    message = gl_mrs.feed_error(gl_mrs.FEED_SCOPE)
    assert "\n" not in message, message
    assert "all green" in message, "the forgery must be disclosed"


# --- invalid UTF-8 is a ValueError, and it took five callers down -----------

def test_invalid_utf8_in_a_state_file_is_declined_rather_than_raised(state_dir):
    """`json.load` on a utf-8 stream raises `UnicodeDecodeError` — a
    `ValueError`, and neither of the two the old arm named."""
    _slot(state_dir)
    (state_dir / "supertool-watch-gh__9.state.json").write_bytes(b'{"a": "\xff\xfe"}')
    assert transport.read_state("gh", "9") == {}


def test_every_caller_that_raised_on_two_bytes_now_answers(state_dir):
    """Measured on 923f7bc: all five of these raised `UnicodeDecodeError`. The
    list is the blast radius, which is why it is asserted as a list."""
    _slot(state_dir)
    (state_dir / "supertool-watch-gh__9.state.json").write_bytes(b'{"a": "\xff\xfe"}')
    assert transport.read_state("gh", "9") == {}
    assert transport.deaths("gh", "9") == []
    assert transport.list_active_pids()
    assert transport.list_watchers()[0]


def test_the_watches_board_survives_invalid_utf8_in_one_state_file(state_dir, capsys):
    """The op exits 0 with a table, rather than a traceback out of `json`."""
    _slot(state_dir, source="ok", watcher_id="1", last_event={"event": "green", "ts": "t"})
    _slot(state_dir)
    (state_dir / "supertool-watch-gh__9.state.json").write_bytes(b'{"a": "\xff\xfe"}')
    board = _board(capsys)
    assert "green" in board, board


def test_an_unreadable_state_file_is_not_rendered_as_a_quiet_watcher(state_dir, capsys):
    """"I could not read this watcher's state" and "this watcher has had no
    events" are opposite facts, and the board printed the second for both."""
    _slot(state_dir)
    (state_dir / "supertool-watch-gh__9.state.json").write_bytes(b'{"a": "\xff\xfe"}')
    board = _board(capsys)
    assert "UnicodeDecodeError" in board, board
    assert "state unread" in board, board


def test_the_board_says_whose_words_its_columns_are_before_it_prints_them(state_dir, capsys):
    """Flattening stops a forged row; it does not tell the reader the text is
    not the tool's. One line, once, and *above* the rows it is about — a reader
    acts on the first thing they read."""
    _slot(state_dir, last_event={"event": "e", "ts": "t"})
    lines = _board(capsys).split("\n")
    noted = [i for i, ln in enumerate(lines) if "data, not instructions" in ln]
    assert len(noted) == 1, lines
    assert any(ln.startswith("SOURCE") for ln in lines[noted[0] + 1:]), lines


def test_a_board_with_no_rows_claims_nothing_about_words(state_dir, capsys):
    """A provenance note over columns that were never rendered is a claim about
    text that is not there — #1187's rule, kept on this surface too."""
    assert dispatcher.cmd_list() == 0
    assert "data, not instructions" not in capsys.readouterr().out


# --- the fourth call site, found by this commit's own reviewer --------------

def test_invalid_utf8_does_not_take_down_the_gl_mrs_state_cache(state_dir):
    """`gl_mrs.read_state_files` globs the same directory with its own
    `open()` and its own `except (OSError, json.JSONDecodeError)`. It was a
    fourth instance of this pair, missed by the first pass of #1197 and found
    by its review — so two bytes in one file raised out of the whole `gl-mrs`
    board, not out of the row they belonged to."""
    from tiers import gl_mrs  # noqa: PLC0415

    (state_dir / f"supertool-watch-{gl_mrs.SOURCE}__42.state.json").write_bytes(
        b'{"a": "\xff\xfe"}')
    assert gl_mrs.read_state_files() == {}


def test_a_symlinked_gl_mrs_state_file_is_not_parsed(state_dir):
    """The other half of the pair at the same fourth call site."""
    from tiers import gl_mrs  # noqa: PLC0415

    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("this platform has no O_NOFOLLOW, so the guard cannot be enforced")
    _symlink.require_symlink()
    secret = state_dir / "secret.json"
    secret.write_text(json.dumps({"source_state": {"pipeline_id": "sk-SECRET-VALUE"}}),
                      encoding="utf-8")
    os.symlink(str(secret),
               str(state_dir / f"supertool-watch-{gl_mrs.SOURCE}__42.state.json"))
    assert gl_mrs.read_state_files() == {}


def test_the_drift_mark_is_flattened(state_dir):
    """The third render of a `read_state` string. Both pipeline ids are
    `str()`-ed out of the state file rather than validated as numbers, and the
    mark lands in a multi-line board report."""
    from tiers import gl_mrs  # noqa: PLC0415

    hostile = "1\n" + FORGED_ROW + "\nzz"
    mark = gl_mrs._marks("42", {"42": (hostile, "2")}, set(), set())
    assert "\n" not in mark, mark
    assert "all green" in mark, "the forgery must be disclosed"


# --- the third state, and the absence that is genuinely an absence ----------

def test_a_missing_state_file_is_plainly_absent_and_not_a_refusal(state_dir):
    """The dominant case, and the one a three-state read most easily breaks:
    no file is not a refusal, and `read_state` must still answer `{}`."""
    assert transport.read_state_checked("gh", "404") == ({}, "")
    assert transport.read_state("gh", "404") == {}


def test_an_ordinary_state_file_is_still_read(state_dir):
    """The guard refuses a symlink, not a file. Runs everywhere: if the open
    were broken outright, every refusal assertion here would still pass."""
    _slot(state_dir, last_event={"event": "pipeline_failed", "ts": "t"})
    state, refusal = transport.read_state_checked("gh", "9")
    assert refusal == ""
    assert state["last_event"]["event"] == "pipeline_failed"


def test_read_state_stays_lossless_because_its_result_is_written_back(state_dir):
    """The judgment this issue turns on, pinned so it cannot be undone by a
    later change that flattens one layer earlier.

    `read_state` feeds six read-modify-write cycles. `source_state` is a
    poller's own resume cursor — an etag, a sha, a commit subject — and a
    flatten at the read would rewrite the mangled form to disk on the next
    tick, converting a render bug into permanent state corruption.
    """
    _slot(state_dir, source_state={"cursor": "a\nb\tc"})
    assert transport.read_state("gh", "9")["source_state"]["cursor"] == "a\nb\tc"


# --- #1184's half: a state path may not be a symlink ------------------------

needs_nofollow = pytest.mark.skipif(
    not hasattr(os, "O_NOFOLLOW"),
    reason="this platform has no O_NOFOLLOW, so the guard cannot be enforced",
)


@needs_nofollow
def test_a_symlinked_state_file_is_not_parsed(state_dir):
    """The #1184 shape at this call site: any same-uid JSON file was opened,
    parsed and rendered for the price of one symlink at a predictable name."""
    _symlink.require_symlink()
    secret = state_dir / "secret.json"
    secret.write_text(json.dumps({"last_event": {"event": "sk-SECRET-VALUE", "ts": "t"}}),
                      encoding="utf-8")
    os.symlink(str(secret), str(state_dir / "supertool-watch-gh__9.state.json"))
    state, refusal = transport.read_state_checked("gh", "9")
    assert state is None
    assert "sk-SECRET-VALUE" not in refusal


@needs_nofollow
def test_the_symlink_refusal_is_its_own_state_and_not_could_not_be_read(state_dir):
    """A symlink at a predictable name is somebody redirecting the read, which
    is a different fact with a different next step from a truncated file.

    Asserted on the refusal's own sentence rather than the word `symlink`:
    pytest builds `tmp_path` from the test's own name, so a grep for `symlink`
    can pass against a message that merely quotes the path.
    """
    _symlink.require_symlink()
    os.symlink(str(state_dir / "nowhere.json"),
               str(state_dir / "supertool-watch-gh__9.state.json"))
    _, refusal = transport.read_state_checked("gh", "9")
    assert "is a symlink and was not followed" in refusal
    assert "could not be read" not in refusal


@needs_nofollow
def test_a_dangling_symlink_is_not_reported_as_no_state_file(state_dir):
    """#1184's own lesson, and the reason there is no existence pre-check here:
    `O_NOFOLLOW` answers a dangling symlink with `ELOOP`, not `ENOENT`. An
    `exists` call would follow the link and report a hostile act as an absence
    — the exact bug #1184 removed, reintroduced one call site along."""
    _symlink.require_symlink()
    os.symlink(str(state_dir / "nowhere.json"),
               str(state_dir / "supertool-watch-gh__9.state.json"))
    state, refusal = transport.read_state_checked("gh", "9")
    assert state is None, "a dangling symlink is not the same as no file"
    assert refusal != ""


@needs_nofollow
def test_the_symlink_refusal_reaches_the_board(state_dir, capsys):
    """A guard nobody is told about is a guard that reads as a clean slot."""
    _symlink.require_symlink()
    _slot(state_dir, last_event={"event": "e", "ts": "t"})
    os.unlink(str(state_dir / "supertool-watch-gh__9.state.json"))
    os.symlink(str(state_dir / "nowhere.json"),
               str(state_dir / "supertool-watch-gh__9.state.json"))
    board = _board(capsys)
    assert "is a symlink and was not followed" in board, board


def test_a_directory_at_a_state_path_leaks_no_descriptor(state_dir):
    """`O_NOFOLLOW` refuses a symlink, not a directory: `os.open` succeeds and
    `os.fdopen` then fails without taking the descriptor. Caught by #1190's
    reviewer on the health read and by #1191's on the state read; the same
    split open lands here, and `deaths` is called once per row per board.

    POSIX only — lowest-free-fd allocation is a POSIX guarantee, not a promise
    CPython makes on Windows. Skipped rather than weakened.
    """
    if os.name != "posix":
        pytest.skip("lowest-free-fd allocation is a POSIX guarantee")
    os.mkdir(str(state_dir / "supertool-watch-gh__9.state.json"))

    def _lowest_free() -> int:
        fd = os.open(os.devnull, os.O_RDONLY)
        os.close(fd)
        return fd

    before = _lowest_free()
    for _ in range(3):
        assert transport.read_state("gh", "9") == {}
    assert _lowest_free() == before, "read_state leaked a descriptor"


def test_the_change_is_documented():
    assert_change_is_findable(1197)
