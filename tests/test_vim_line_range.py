"""Tests for :Ns and :N,Ms line-range substitute in op_vim (vim parity).

Real vim supports:
- `:Ns/PAT/REPL/[flags]`     — substitute on line N only
- `:N,Ms/PAT/REPL/[flags]`   — substitute on lines N through M (inclusive)
- `:.s/...`                  — substitute on current line (cursor's line)
- `:$s/...`                  — substitute on last line
- `:.,$s/...`                — current line through end
"""
from __future__ import annotations

from pathlib import Path

import supertool


def test_single_line_substitute(tmp_path: Path) -> None:
    """`:3s/foo/X/g` runs only on line 3."""
    f = tmp_path / "x.txt"
    f.write_text("foo\nfoo\nfoo\nfoo\n")
    out = supertool.op_vim(str(f), ":3s/foo/X/g")
    assert "ERROR" not in out, out
    # Only line 3 changed
    assert f.read_text(encoding="utf-8") == "foo\nfoo\nX\nfoo\n"


def test_line_range_substitute(tmp_path: Path) -> None:
    """`:2,3s/foo/X/g` runs on lines 2 and 3."""
    f = tmp_path / "x.txt"
    f.write_text("foo\nfoo\nfoo\nfoo\n")
    out = supertool.op_vim(str(f), ":2,3s/foo/X/g")
    assert "ERROR" not in out, out
    assert f.read_text(encoding="utf-8") == "foo\nX\nX\nfoo\n"


def test_line_range_substitute_first_only(tmp_path: Path) -> None:
    """`:2,3s/foo/X/` (no /g flag) replaces first match per line in range."""
    f = tmp_path / "x.txt"
    f.write_text("foo foo\nfoo foo\nfoo foo\nfoo foo\n")
    out = supertool.op_vim(str(f), ":2,3s/foo/X/")
    assert "ERROR" not in out, out
    assert f.read_text(encoding="utf-8") == "foo foo\nX foo\nX foo\nfoo foo\n"


def test_dollar_means_last_line(tmp_path: Path) -> None:
    """`:$s/foo/X/g` runs on the last line."""
    f = tmp_path / "x.txt"
    f.write_text("foo\nfoo\nfoo\n")
    out = supertool.op_vim(str(f), ":$s/foo/X/g")
    assert "ERROR" not in out, out
    assert f.read_text(encoding="utf-8") == "foo\nfoo\nX\n"


def test_dot_means_current_line(tmp_path: Path) -> None:
    """`:.s/foo/X/g` runs on the cursor's current line."""
    f = tmp_path / "x.txt"
    f.write_text("foo\nfoo\nfoo\n")
    out = supertool.op_vim(str(f), "2G␞:.s/foo/X/g")
    assert "ERROR" not in out, out
    # Cursor on line 2, only that line changes
    assert f.read_text(encoding="utf-8") == "foo\nX\nfoo\n"


def test_dot_to_dollar_range(tmp_path: Path) -> None:
    """`:.,$s/foo/X/g` runs from current line to last line."""
    f = tmp_path / "x.txt"
    f.write_text("foo\nfoo\nfoo\nfoo\n")
    out = supertool.op_vim(str(f), "3G␞:.,$s/foo/X/g")
    assert "ERROR" not in out, out
    # Lines 3 and 4 changed
    assert f.read_text(encoding="utf-8") == "foo\nfoo\nX\nX\n"


def test_line_range_out_of_bounds_errors_clearly(tmp_path: Path) -> None:
    """`:99s/foo/X/g` on a 3-line file should error, not silently no-op."""
    f = tmp_path / "x.txt"
    f.write_text("foo\nfoo\nfoo\n")
    out = supertool.op_vim(str(f), ":99s/foo/X/g")
    assert "ERROR" in out


def test_line_range_inverted_errors(tmp_path: Path) -> None:
    """`:5,2s/...` (end before start) should error."""
    f = tmp_path / "x.txt"
    f.write_text("foo\nfoo\nfoo\nfoo\nfoo\n")
    out = supertool.op_vim(str(f), ":5,2s/foo/X/g")
    assert "ERROR" in out


def test_whole_file_alias_still_works(tmp_path: Path) -> None:
    """Regression — `:%s/foo/X/g` (whole-file alias) unchanged."""
    f = tmp_path / "x.txt"
    f.write_text("foo\nfoo\nfoo\n")
    out = supertool.op_vim(str(f), ":%s/foo/X/g")
    assert "ERROR" not in out, out
    assert f.read_text(encoding="utf-8") == "X\nX\nX\n"


def test_bare_s_still_means_whole_file(tmp_path: Path) -> None:
    """Regression — `:s/foo/X/g` (no range) — current supertool behavior is
    whole-file. Vim's bare `:s` is current-line-only; we preserve the
    existing whole-file semantics to avoid silently breaking the corpus."""
    f = tmp_path / "x.txt"
    f.write_text("foo\nfoo\nfoo\n")
    out = supertool.op_vim(str(f), ":s/foo/X/g")
    assert "ERROR" not in out, out
    assert f.read_text(encoding="utf-8") == "X\nX\nX\n"
