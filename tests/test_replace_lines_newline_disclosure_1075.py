"""#1075 - `replace_lines` can turn a uniform file mixed and disclose nothing.

Class: misreports. Every byte the caller typed is on disk; the receipt is what
is wrong.

`_newline_note`'s mixed-file branch only ever saw the file *before* the write,
so a write that CREATES mixedness could not reach it. An all-CRLF file plus an
LF replacement block came back mixed under a receipt that said nothing.

Two shapes, and the second is the one hiding behind the first:

* the block is unterminated, so the op invents a trailing ending  - the issue's
  own repro;
* the block already ends a line, so the op invents nothing at all - `wrote` is
  empty, the note short-circuits before the census, and the same mixed file
  ships with the same silence.

The counts have to describe the file on disk. `4 CRLF / 0 LF` is the file that
no longer exists.

The noise side is pinned too: a write that leaves the file uniform stays
silent, because a marker that fires on every CRLF edit is one a reader learns
to skip.
"""

from __future__ import annotations

from pathlib import Path

import _supertool


def _note(out: str) -> str:
    return "".join(ln for ln in out.splitlines(keepends=True)
                   if "line endings" in ln)


def test_lf_block_into_a_crlf_file_discloses_the_mix(tmp_path: Path) -> None:
    """The issue's repro: nothing is lost, and nothing is said."""
    f = tmp_path / "f.txt"
    f.write_bytes(b"1\r\n2\r\n3\r\n4\r\n")
    out = _supertool.op_replace_lines(str(f), 2, 3, "TWO\nTHREE")
    assert "ERROR" not in out, out
    assert f.read_bytes() == b"1\r\nTWO\nTHREE\r\n4\r\n"
    assert "mixed" in _note(out), out


def test_the_census_describes_the_file_on_disk(tmp_path: Path) -> None:
    """Pre-write the file was 4 CRLF / 0 LF. That file is gone."""
    f = tmp_path / "f.txt"
    f.write_bytes(b"1\r\n2\r\n3\r\n4\r\n")
    out = _supertool.op_replace_lines(str(f), 2, 3, "TWO\nTHREE")
    assert "3 CRLF / 1 LF / 0 CR" in _note(out), out


def test_terminated_lf_block_into_a_crlf_file_discloses_too(
    tmp_path: Path,
) -> None:
    """No ending is invented here, so the old code never even ran the census -
    and the file is just as mixed."""
    f = tmp_path / "f.txt"
    f.write_bytes(b"1\r\n2\r\n3\r\n4\r\n")
    out = _supertool.op_replace_lines(str(f), 2, 3, "TWO\nTHREE\n")
    assert "ERROR" not in out, out
    assert f.read_bytes() == b"1\r\nTWO\nTHREE\n4\r\n"
    note = _note(out)
    assert "mixed" in note, out
    assert "2 CRLF / 2 LF / 0 CR" in note, out


def test_insert_of_an_lf_block_into_a_crlf_file_discloses(
    tmp_path: Path,
) -> None:
    f = tmp_path / "f.txt"
    f.write_bytes(b"1\r\n2\r\n")
    out = _supertool.op_replace_lines(str(f), 2, 1, "NEW\n")
    assert f.read_bytes() == b"1\r\nNEW\n2\r\n"
    assert "mixed" in _note(out), out


def test_uniform_lf_write_stays_silent(tmp_path: Path) -> None:
    f = tmp_path / "f.txt"
    f.write_bytes(b"1\n2\n3\n")
    out = _supertool.op_replace_lines(str(f), 2, 2, "TWO")
    assert f.read_bytes() == b"1\nTWO\n3\n"
    assert _note(out) == "", out


def test_uniform_crlf_write_stays_silent(tmp_path: Path) -> None:
    """A CRLF block into a CRLF file decides nothing. On Windows every file is
    CRLF, so a note here would fire on every call ever made."""
    f = tmp_path / "f.txt"
    f.write_bytes(b"1\r\n2\r\n3\r\n")
    out = _supertool.op_replace_lines(str(f), 2, 2, "TWO\r\n")
    assert f.read_bytes() == b"1\r\nTWO\r\n3\r\n"
    assert _note(out) == "", out


def test_deleting_lines_from_a_uniform_file_stays_silent(
    tmp_path: Path,
) -> None:
    """A delete writes no endings and cannot create a mix."""
    f = tmp_path / "f.txt"
    f.write_bytes(b"1\r\n2\r\n3\r\n")
    out = _supertool.op_replace_lines(str(f), 2, 2, "")
    assert f.read_bytes() == b"1\r\n3\r\n"
    assert _note(out) == "", out


def test_edit_still_reterms_and_still_says_so(tmp_path: Path) -> None:
    """The sibling call site must not change behaviour: op_edit resolves the
    convention before writing, so its file stays uniform and its receipt keeps
    the re-terminated disclosure rather than acquiring a mixed one."""
    f = tmp_path / "f.txt"
    f.write_bytes(b"a\r\nb\r\nc\r\n")
    out = _supertool.op_edit("a\nb", "a\nB", str(f))
    assert "ERROR" not in out, out
    assert f.read_bytes() == b"a\r\nB\r\nc\r\n"
    note = _note(out)
    assert "re-terminated" in note, out
    assert "mixed" not in note, out


def test_edit_on_an_already_mixed_file_reports_the_post_write_census(
    tmp_path: Path,
) -> None:
    f = tmp_path / "f.txt"
    f.write_bytes(b"a\r\nb\nc\r\n")
    out = _supertool.op_edit("b\nc", "B\nc", str(f))
    assert "ERROR" not in out, out
    note = _note(out)
    assert "mixed" in note, out
    crlf, lf, cr = _supertool._newline_census(
        f.read_bytes().decode("utf-8"))
    assert f"{crlf} CRLF / {lf} LF / {cr} CR" in note, out
