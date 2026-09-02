"""#1085 - `append` hardcodes an LF terminator and never discloses.

Class: misreports / silently mutates. `op_append` never went through the
newline-convention machinery #1073/#1075 built for `op_edit` and
`op_replace_lines`: the terminator it invents for an unterminated `content`
was always "\n", regardless of what convention the target file already used,
and it never called `_newline_note` at all -- so an append that turns a
uniform file into a mixed one shipped with no disclosure, even in principle.

The fix follows the same shape #1075 already established for
`replace_lines`: the invented terminator adopts the file's own convention
(scanning backwards from EOF, via `_local_newline`), and disclosure is
computed from the file **as written**, not as it was found.

The caller's own text is never rewritten -- only the ending this op has to
*invent* when `content` has none. `test_append_preserves_crlf_in_existing_content`
(#383) already pins that a caller-terminated block goes in verbatim; nothing
here changes that.
"""

from __future__ import annotations

from pathlib import Path

import _supertool


def _note(out: str) -> str:
    return "".join(ln for ln in out.splitlines(keepends=True)
                   if "line endings" in ln)


def test_append_adopts_crlf_for_the_terminator_it_invents(tmp_path: Path) -> None:
    """The issue's own repro: content has no trailing newline, file is CRLF."""
    f = tmp_path / "f.txt"
    f.write_bytes(b"1\r\n2\r\n")
    out = _supertool.op_append(str(f), "three")
    assert "ERROR" not in out, out
    assert f.read_bytes() == b"1\r\n2\r\nthree\r\n", f.read_bytes()
    # A single convention throughout: nothing to disclose.
    assert _note(out) == "", out


def test_append_that_creates_a_mixed_file_discloses_it(tmp_path: Path) -> None:
    """A file already mixed before the append stays mixed after it, and now
    says so -- computed from the post-write bytes, not the pre-write ones."""
    f = tmp_path / "f.txt"
    f.write_bytes(b"1\r\n2\n")  # already mixed: one CRLF line, one LF line
    out = _supertool.op_append(str(f), "three")
    assert "ERROR" not in out, out
    note = _note(out)
    assert "mixed" in note, out
    assert "1 CRLF" in note and "2 LF" in note, note


def test_leading_newline_in_content_survives(tmp_path: Path) -> None:
    """A `content` opening with its own newline (e.g. to open a new paragraph)
    is not eaten by the trailing-newline normalisation."""
    f = tmp_path / "notes.md"
    f.write_bytes(b"first paragraph\n")
    out = _supertool.op_append(str(f), "\nsecond paragraph\n")
    assert "ERROR" not in out, out
    assert f.read_bytes() == b"first paragraph\n\nsecond paragraph\n", f.read_bytes()


def test_ordinary_append_unaffected(tmp_path: Path) -> None:
    """A plain LF-terminated append to a plain LF file: no regression."""
    f = tmp_path / "notes.md"
    f.write_bytes(b"line1\nline2\n")
    out = _supertool.op_append(str(f), "## New section\n")
    assert "ERROR" not in out, out
    assert f.read_bytes() == b"line1\nline2\n## New section\n"
    assert _note(out) == "", out


def test_append_to_new_file_still_uses_lf(tmp_path: Path) -> None:
    """No file to take a convention from: falls back to LF, same as before."""
    f = tmp_path / "brand_new.txt"
    out = _supertool.op_append(str(f), "hello")
    assert "ERROR" not in out, out
    assert f.read_bytes() == b"hello\n"
