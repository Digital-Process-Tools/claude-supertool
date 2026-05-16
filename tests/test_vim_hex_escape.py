"""Tests for \\xHH hex escape inside TEXT (op_vim).

Kevin reaches for `\\x27` (single quote) to avoid bash single-quote
nesting hell. Real shells decode `\\xHH` to a single byte; we match that
for TEXT args.
"""
from __future__ import annotations

from pathlib import Path

import supertool


def test_x27_decodes_to_single_quote(tmp_path: Path) -> None:
    """`\\x27` (U+0027) → `'`."""
    f = tmp_path / "x.txt"
    f.write_text("end\n")
    supertool.op_vim(str(f), "iIt\\x27s done. ")
    assert f.read_text() == "It's done. end\n"


def test_x22_decodes_to_double_quote(tmp_path: Path) -> None:
    """`\\x22` → `\"`."""
    f = tmp_path / "x.txt"
    f.write_text("end\n")
    supertool.op_vim(str(f), "i\\x22hello\\x22 ")
    assert f.read_text() == '"hello" end\n'


def test_x60_decodes_to_backtick(tmp_path: Path) -> None:
    """`\\x60` → backtick."""
    f = tmp_path / "x.md"
    f.write_text("end\n")
    supertool.op_vim(str(f), "i\\x60code\\x60 ")
    assert f.read_text() == "`code` end\n"


def test_x_in_normal_text_stays_literal(tmp_path: Path) -> None:
    """Regression — `\\x` not followed by 2 hex digits stays literal."""
    f = tmp_path / "x.txt"
    f.write_text("end\n")
    supertool.op_vim(str(f), "i\\xzz ")
    # \xzz not a valid hex escape — stays as-is
    assert f.read_text() == "\\xzz end\n"


def test_uppercase_hex_digits_work(tmp_path: Path) -> None:
    """`\\xAB`, `\\xff`, mixed case — all decode."""
    f = tmp_path / "x.txt"
    f.write_text("\n")
    supertool.op_vim(str(f), "i\\x41\\x42\\x43")  # ABC
    assert f.read_text() == "ABC\n"


def test_double_backslash_x_stays_literal(tmp_path: Path) -> None:
    """Regression — `\\\\x27` (double-escaped) decodes to literal `\\x27`."""
    f = tmp_path / "x.txt"
    f.write_text("end\n")
    supertool.op_vim(str(f), "i\\\\x27 ")
    # `\\\\` → `\\` (literal backslash), then `x27` stays literal text
    assert f.read_text() == "\\x27 end\n"
