"""#1049 — `op_edit` rewrote every line ending in a CRLF file to LF.

`op_edit` opened the target without `newline=""`, so Python's universal-newline
translation flattened every `\\r\\n` to `\\n` in memory; `_atomic_write` then put
those bytes back. A one-line edit produced a whole-file diff, under a receipt
that named a single line. `op_append` already opens with `newline=""` and says
in a comment why. `op_replace` and `op_replace_lines` had the same gap.

Every assertion here is on **bytes**. A text post-condition decodes `\\r\\n` to
`\\n` and would call a CRLF file equal to an LF one, which is exactly how this
could go green over a write of the wrong bytes.

Mixed-ending files have no single right answer, so the pins state the one that
was chosen: untouched bytes are preserved verbatim, inserted text adopts the
ending of the line it lands next to, and the receipt says which was used rather
than choosing in silence.
"""

from __future__ import annotations

from pathlib import Path

import _supertool


def _crlf(tmp_path: Path, name: str = "w.txt") -> Path:
    f = tmp_path / name
    f.write_bytes(b"alpha\r\nbeta\r\ngamma\r\ndelta\r\n")
    return f


# --- op_edit ---------------------------------------------------------------


def test_edit_of_a_crlf_file_touches_only_the_edited_line(tmp_path: Path) -> None:
    f = _crlf(tmp_path)
    out = _supertool.op_edit("beta", "BETA", str(f))
    assert not out.startswith("ERROR"), out
    assert f.read_bytes() == b"alpha\r\nBETA\r\ngamma\r\ndelta\r\n"


def test_multiline_edit_written_with_lf_still_applies_to_a_crlf_file(
    tmp_path: Path,
) -> None:
    """A caller composing a payload writes LF. The old string therefore does not
    match a CRLF file byte-for-byte. Preserving endings must not turn a working
    edit into `old string not found` — and the replacement must go in with the
    file's endings, not the caller's."""
    f = _crlf(tmp_path)
    out = _supertool.op_edit("beta\ngamma", "BETA\nGAMMA", str(f))
    assert not out.startswith("ERROR"), out
    assert f.read_bytes() == b"alpha\r\nBETA\r\nGAMMA\r\ndelta\r\n"
    assert "CRLF" in out, out


def test_edit_of_an_lf_file_is_unaffected(tmp_path: Path) -> None:
    f = tmp_path / "u.txt"
    f.write_bytes(b"alpha\nbeta\ngamma\n")
    out = _supertool.op_edit("beta", "BETA", str(f))
    assert not out.startswith("ERROR"), out
    assert f.read_bytes() == b"alpha\nBETA\ngamma\n"
    assert "CRLF" not in out


def test_edit_of_a_mixed_file_preserves_the_lines_it_did_not_touch(
    tmp_path: Path,
) -> None:
    """No global convention is imposed: the CRLF lines stay CRLF and the LF
    lines stay LF, because neither is the file's answer."""
    f = tmp_path / "m.txt"
    f.write_bytes(b"one\r\ntwo\nthree\r\nfour\n")
    out = _supertool.op_edit("two", "TWO", str(f))
    assert not out.startswith("ERROR"), out
    assert f.read_bytes() == b"one\r\nTWO\nthree\r\nfour\n"
    assert "mixed" in out, out


# --- op_replace ------------------------------------------------------------


def test_replace_across_a_crlf_file_touches_only_the_matches(
    tmp_path: Path,
) -> None:
    f = _crlf(tmp_path)
    out = _supertool.op_replace("beta", "BETA", str(tmp_path))
    assert "ERROR" not in out, out
    assert f.read_bytes() == b"alpha\r\nBETA\r\ngamma\r\ndelta\r\n"


# --- op_replace_lines ------------------------------------------------------


def test_replace_lines_in_a_crlf_file_keeps_every_other_line_crlf(
    tmp_path: Path,
) -> None:
    f = _crlf(tmp_path)
    out = _supertool.op_replace_lines(str(f), 2, 2, "BETA")
    assert not out.startswith("ERROR"), out
    assert f.read_bytes() == b"alpha\r\nBETA\r\ngamma\r\ndelta\r\n"


def test_replace_lines_insert_into_a_crlf_file_uses_crlf(tmp_path: Path) -> None:
    f = _crlf(tmp_path)
    out = _supertool.op_replace_lines(str(f), 3, 0, "inserted")
    assert not out.startswith("ERROR"), out
    assert f.read_bytes() == (
        b"alpha\r\nbeta\r\ninserted\r\ngamma\r\ndelta\r\n"
    )


def test_replace_lines_deletion_in_a_crlf_file_leaves_crlf_behind(
    tmp_path: Path,
) -> None:
    f = _crlf(tmp_path)
    out = _supertool.op_replace_lines(str(f), 2, 3, "")
    assert not out.startswith("ERROR"), out
    assert f.read_bytes() == b"alpha\r\ndelta\r\n"


def test_replace_lines_in_a_mixed_file_takes_the_local_ending_and_says_so(
    tmp_path: Path,
) -> None:
    """Line 2 ends LF, so the block that replaces it ends LF — the neighbouring
    CRLF lines are not evidence about it, and a file-wide majority vote would
    silently rewrite the caller's line to the other convention."""
    f = tmp_path / "m.txt"
    f.write_bytes(b"one\r\ntwo\nthree\r\nfour\r\n")
    out = _supertool.op_replace_lines(str(f), 2, 2, "TWO")
    assert not out.startswith("ERROR"), out
    assert f.read_bytes() == b"one\r\nTWO\nthree\r\nfour\r\n"
    assert "mixed" in out, out


# --- the receipt must not be the only thing that is true -------------------


def test_op_append_still_preserves_crlf(tmp_path: Path) -> None:
    """The one op that already had it. Pinned so the shared helper cannot
    regress it while fixing the others."""
    f = _crlf(tmp_path)
    out = _supertool.op_append(str(f), "epsilon")
    assert not out.startswith("ERROR"), out
    assert f.read_bytes().startswith(b"alpha\r\nbeta\r\ngamma\r\ndelta\r\n")
