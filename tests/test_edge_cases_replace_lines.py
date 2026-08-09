"""Edge-case tests for op_replace_lines.

Each test documents the observed contract (or bug). Where a discrepancy
between documented and observed behaviour is found, a BUG comment is
attached and the test pins the *observed* behaviour so regressions are
caught.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from _symlink import requires_symlink

import supertool


FIVE_LINES = "alpha\nbeta\ngamma\ndelta\nepsilon\n"


# ---------------------------------------------------------------------------
# 1. START > END — but both are in valid range (not insert mode)
#    end < start is the *insert* mode signal. So start=3, end=2 is insert.
#    start=5, end=2 is also treated as insert (end < start regardless of
#    magnitude). There is no "bad range" error for end < start — it's always
#    interpreted as insert-before-start.
# ---------------------------------------------------------------------------

def test_start_greater_than_end_treated_as_insert(tmp_path: Path) -> None:
    """start=4, end=2 — end < start so treated as insert before line 4.

    Contract: insert mode fires whenever end < start.  No error is raised.
    The content is inserted before line `start`; lines [2..3] are NOT removed.
    """
    f = tmp_path / "f.txt"
    f.write_text(FIVE_LINES)
    out = supertool.op_replace_lines(str(f), 4, 2, "INSERTED\n")
    # Must NOT be an error
    assert "ERROR" not in out, f"Unexpected error: {out}"
    lines = f.read_text(encoding="utf-8").splitlines()
    # Inserted before original line 4 ("delta")
    assert "INSERTED" in lines
    insert_pos = lines.index("INSERTED")
    assert lines[insert_pos + 1] == "delta", (
        "insert before line 4 means 'delta' should follow immediately"
    )
    # Lines 1-3 and 5 preserved
    assert "alpha" in lines
    assert "beta" in lines
    assert "gamma" in lines
    assert "epsilon" in lines


# ---------------------------------------------------------------------------
# 2. END >> total line count (not just total+1 which autoclamped)
# ---------------------------------------------------------------------------

def test_end_far_beyond_total_errors(tmp_path: Path) -> None:
    """end=99999 on 5-line file — should be a clean ERROR, not a crash."""
    f = tmp_path / "f.txt"
    f.write_text(FIVE_LINES)
    out = supertool.op_replace_lines(str(f), 1, 99999, "NEW\n")
    assert "ERROR" in out
    assert f.read_text(encoding="utf-8") == FIVE_LINES, "File must be unchanged after error"


def test_end_equals_total_plus_one_autoclamped(tmp_path: Path) -> None:
    """end = total+1 (6 on 5-line file) is autoclamped with a hint in receipt."""
    f = tmp_path / "f.txt"
    f.write_text(FIVE_LINES)
    out = supertool.op_replace_lines(str(f), 3, 6, "NEW\n")
    assert "ERROR" not in out
    assert "autocorrect" in out or "clamped" in out, (
        "Receipt should mention the autocorrect that happened"
    )
    lines = f.read_text(encoding="utf-8").splitlines()
    assert lines[2] == "NEW"
    assert len(lines) == 3  # lines 1-2 + NEW


# ---------------------------------------------------------------------------
# 3. START = 0 — 1-indexed contract: must be a clean error
# ---------------------------------------------------------------------------

def test_start_zero_is_error(tmp_path: Path) -> None:
    """start=0 violates 1-indexed contract — clean ERROR, no crash, no write."""
    f = tmp_path / "f.txt"
    f.write_text(FIVE_LINES)
    out = supertool.op_replace_lines(str(f), 0, 3, "NEW\n")
    assert "ERROR" in out
    assert f.read_text(encoding="utf-8") == FIVE_LINES, "File must be unchanged"


# ---------------------------------------------------------------------------
# 4. Negative START
# ---------------------------------------------------------------------------

def test_negative_start_is_error(tmp_path: Path) -> None:
    """start=-1 — clean ERROR, file unchanged."""
    f = tmp_path / "f.txt"
    f.write_text(FIVE_LINES)
    out = supertool.op_replace_lines(str(f), -1, 3, "NEW\n")
    assert "ERROR" in out
    assert f.read_text(encoding="utf-8") == FIVE_LINES, "File must be unchanged"


def test_negative_start_and_end_both_error(tmp_path: Path) -> None:
    """start=-5, end=-2 — both negative, expect ERROR."""
    f = tmp_path / "f.txt"
    f.write_text(FIVE_LINES)
    out = supertool.op_replace_lines(str(f), -5, -2, "NEW\n")
    assert "ERROR" in out
    assert f.read_text(encoding="utf-8") == FIVE_LINES


# ---------------------------------------------------------------------------
# 5. Content with mixed line endings (CRLF + LF)
#    The op appends "\n" if content doesn't end with "\n", but should not
#    normalise internal line endings.
# ---------------------------------------------------------------------------

def test_mixed_line_endings_preserved(tmp_path: Path) -> None:
    """Content with CRLF+LF mixed should survive round-trip as-is.

    The op only adds a trailing \\n if missing — it must not normalise
    internal \\r\\n to \\n.
    """
    f = tmp_path / "f.txt"
    f.write_text(FIVE_LINES)
    mixed = "line_a\r\nline_b\nline_c\r\n"
    out = supertool.op_replace_lines(str(f), 2, 3, mixed)
    assert "ERROR" not in out
    raw = f.read_bytes()
    # CRLF sequences from our content must survive
    assert b"line_a\r\n" in raw, "CRLF from content should be preserved verbatim"
    assert b"line_b\n" in raw, "LF-only line should survive too"


# ---------------------------------------------------------------------------
# 6. Empty content + END < START (insert empty at position)
#    end < start = insert mode; empty content = insert nothing.
#    Expected: no-op on file contents, receipt confirms 0 lines inserted.
# ---------------------------------------------------------------------------

def test_insert_empty_content_noop(tmp_path: Path) -> None:
    """Insert mode (end < start) with empty content — file unchanged.

    The content block is empty so nothing is injected; the file should
    remain identical to the original.
    """
    f = tmp_path / "f.txt"
    f.write_text(FIVE_LINES)
    original = f.read_text(encoding="utf-8")
    out = supertool.op_replace_lines(str(f), 3, 2, "")
    assert "ERROR" not in out
    assert f.read_text(encoding="utf-8") == original, (
        "Inserting empty string should leave file unchanged"
    )


# ---------------------------------------------------------------------------
# 7. Replace lines on a symlink — symlink survives (_atomic_write PR #137 fix)
# ---------------------------------------------------------------------------

@requires_symlink
def test_replace_lines_on_symlink(tmp_path: Path) -> None:
    """_atomic_write follows realpath so the symlink is NOT replaced by a
    regular file.  The symlink path must still exist as a symlink after the op.
    """
    real = tmp_path / "real.txt"
    real.write_text(FIVE_LINES)
    link = tmp_path / "link.txt"
    link.symlink_to(real)

    out = supertool.op_replace_lines(str(link), 2, 3, "REPLACED\n")
    assert "ERROR" not in out

    # Symlink must still be a symlink
    assert link.is_symlink(), "Symlink must survive the atomic write"
    # Both paths should read the new content
    assert "REPLACED" in link.read_text(encoding="utf-8")
    assert "REPLACED" in real.read_text(encoding="utf-8")
    # Other lines intact
    assert "alpha" in real.read_text(encoding="utf-8")
    assert "delta" in real.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 8. File with surrogate-escape bytes in *untouched* region
#    Read uses errors="replace" (replaces lone bytes with U+FFFD) while
#    _atomic_write uses errors="surrogateescape".
#    BUG: the read step uses errors="replace" not "surrogateescape", so lone
#    high bytes in untouched lines are silently replaced with U+FFFD before
#    the write even occurs.  The test pins the *observed* behaviour.
# ---------------------------------------------------------------------------

def test_surrogate_bytes_in_untouched_region(tmp_path: Path) -> None:
    """File contains a lone \\xc3 byte (invalid UTF-8) in line 1 (untouched).
    We replace only line 3.

    BUG (supertool.py:2616): read() uses errors='replace', not
    errors='surrogateescape'.  The lone \\xc3 in line 1 is replaced with the
    U+FFFD replacement character (\\xef\\xbf\\xbd in UTF-8) before it ever
    reaches _atomic_write, so the original byte is permanently lost.

    This test pins the observed (broken) behaviour.  When fixed, the assert
    should be changed to verify \\xc3 is preserved.
    """
    f = tmp_path / "binary_ish.txt"
    # Line 1 has a lone 0xc3 byte (start of a 2-byte UTF-8 seq, never completed)
    f.write_bytes(b"\xc3line1\nbeta\ngamma\ndelta\nepsilon\n")

    out = supertool.op_replace_lines(str(f), 3, 3, "REPLACED\n")
    assert "ERROR" not in out

    raw_after = f.read_bytes()
    # Fixed 2026-05-23: read uses errors='surrogateescape', lone \xc3 survives.
    assert raw_after == b"\xc3line1\nbeta\nREPLACED\ndelta\nepsilon\n"
    assert b"\xef\xbf\xbd" not in raw_after, "U+FFFD must not appear"


# ---------------------------------------------------------------------------
# 9. Path with NUL byte — clean ERROR, no crash
# ---------------------------------------------------------------------------

def test_path_with_nul_byte_errors_cleanly(tmp_path: Path) -> None:
    """A path containing \\x00 is invalid on all POSIX systems.
    The op should return a clean ERROR string, not raise ValueError/TypeError.

    On Python 3.14+, os.path.isfile() returns False for NUL-containing paths
    (instead of raising ValueError as it did on older versions), so the op
    already returns a clean 'ERROR: file not found' string.  This test pins
    that clean-error behaviour.
    """
    nul_path = str(tmp_path / "file\x00name.txt")
    out = supertool.op_replace_lines(nul_path, 1, 1, "x\n")
    assert "ERROR" in out


# ---------------------------------------------------------------------------
# 10. Replace ALL lines (1:total) with empty content — file becomes empty
# ---------------------------------------------------------------------------

def test_replace_all_lines_with_empty_makes_empty_file(tmp_path: Path) -> None:
    """Replacing every line with empty content should produce a 0-byte file,
    not delete the file.
    """
    f = tmp_path / "f.txt"
    f.write_text(FIVE_LINES)
    total = len(FIVE_LINES.splitlines())  # 5

    out = supertool.op_replace_lines(str(f), 1, total, "")
    assert "ERROR" not in out

    # File must still exist
    assert f.exists(), "File must not be deleted"
    assert f.read_text(encoding="utf-8") == "", f"File should be empty, got: {f.read_text(encoding='utf-8')!r}"
