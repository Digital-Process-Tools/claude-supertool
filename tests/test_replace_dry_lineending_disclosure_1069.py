"""#1069 — replace_dry says nothing when the caller's `old` was silently
re-terminated to match a file's line-ending convention.

The real (non-dry) `op_replace` discloses this per file since #1057/#1049 —
`_supertool.py`'s `retermed` dict in the execute branch, keyed off `eff_old
!= old`. `replace_dry` computes the exact same `eff_old`/`eff_new` pair
one branch earlier (it is the same scan loop, shared by both branches) and
said nothing: a caller previewing a CRLF file with an LF `old` saw a clean
diff with no hint that what would actually be matched on write is not the
literal text they typed.

Fixed by carrying the same per-file `retermed` tag and trailing note into the
dry branch, worded for a preview ("would be re-terminated") rather than a
completed write ("was re-terminated"). Mirrors #1057's own constraint: the
note is per file, next to the file heading it is true of, never a summary
line that would be false of one file in a multi-file run.
"""

from __future__ import annotations

from pathlib import Path

import _supertool


def test_dry_run_discloses_a_silent_reterm_on_a_crlf_file(tmp_path: Path) -> None:
    """The live shape: an `old` spanning a line boundary, written LF, against
    a file that is uniformly CRLF. The preview must say the match it is
    diffing is not the caller's own bytes."""
    f = tmp_path / "a.txt"
    f.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")
    out = _supertool.op_replace("beta\ngamma", "BETA\nGAMMA", str(tmp_path), dry=True)
    assert "ERROR" not in out, out
    assert "line endings" in out, out
    assert "[CRLF]" in out, out
    # The file must be untouched -- this is a preview.
    assert f.read_bytes() == b"alpha\r\nbeta\r\ngamma\r\n"


def test_dry_run_stays_silent_when_old_already_matches_literally(
    tmp_path: Path,
) -> None:
    """The paired 'must fire' case for the assertion above: a single-line
    `old` matches a CRLF file byte for byte with no re-termination needed, so
    nothing was decided on the caller's behalf and the note must not appear.
    Without this, an assertion that the note fires on the CRLF fixture could
    pass even if the note fired unconditionally on every CRLF file."""
    f = tmp_path / "b.txt"
    f.write_bytes(b"one\r\ntwo\r\n")
    out = _supertool.op_replace("two", "TWO", str(tmp_path), dry=True)
    assert "ERROR" not in out, out
    assert "line endings" not in out, out
    assert "[CRLF]" not in out, out
