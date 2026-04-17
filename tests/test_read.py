from __future__ import annotations

from pathlib import Path

import supertool


def test_read_returns_line_numbered_content(tmp_path: Path) -> None:
    f = tmp_path / "hello.py"
    f.write_text("line1\nline2\nline3\n")
    out = supertool.op_read(str(f))
    assert "(3 lines, 18 bytes)" in out
    assert "     1→line1" in out
    assert "     3→line3" in out


def test_read_missing_file_returns_error(tmp_path: Path) -> None:
    out = supertool.op_read(str(tmp_path / "nope.py"))
    assert "ERROR: file not found" in out


def test_read_empty_path_returns_error() -> None:
    out = supertool.op_read("")
    assert "ERROR: file not found" in out


def test_read_complete_file_marker(tmp_path: Path) -> None:
    f = tmp_path / "small.py"
    f.write_text("x = 1\n")
    out = supertool.op_read(str(f))
    assert "[complete file — no more lines]" in out


def test_read_no_complete_marker_when_truncated(tmp_path: Path) -> None:
    f = tmp_path / "many.txt"
    f.write_text("\n".join(f"line{i}" for i in range(1, 11)) + "\n")
    out = supertool.op_read(str(f), offset=0, limit=3)
    assert "[complete file" not in out
    assert "more lines" in out


def test_read_with_offset_and_limit(tmp_path: Path) -> None:
    f = tmp_path / "many.txt"
    f.write_text("\n".join(f"line{i}" for i in range(1, 11)) + "\n")
    out = supertool.op_read(str(f), offset=3, limit=2)
    assert "     4→line4" in out
    assert "     5→line5" in out
    assert "line3" not in out
    assert "line6" not in out


def test_read_truncates_at_byte_cap(tmp_path: Path) -> None:
    f = tmp_path / "big.txt"
    # Write more than 20KB of content: 500 lines × ~100 chars = ~50KB
    f.write_text(("x" * 100 + "\n") * 500)
    out = supertool.op_read(str(f))
    assert "truncated at" in out
    assert "20000 bytes" in out


def test_read_reports_more_lines_available(tmp_path: Path) -> None:
    f = tmp_path / "long.txt"
    f.write_text("\n".join(f"line{i}" for i in range(1, 51)) + "\n")
    out = supertool.op_read(str(f), offset=0, limit=10)
    assert "more lines" in out


def test_read_directory_returns_error(tmp_path: Path) -> None:
    out = supertool.op_read(str(tmp_path))
    assert "ERROR: file not found" in out


def test_read_grep_filter(tmp_path: Path) -> None:
    f = tmp_path / "code.php"
    f.write_text("<?php\nuse Foo;\nuse Bar;\nclass X {\n}\n")
    out = supertool.op_read(str(f), grep_filter="use")
    assert "use Foo" in out
    assert "use Bar" in out
    assert "class X" not in out


def test_read_grep_filter_preserves_line_numbers(tmp_path: Path) -> None:
    f = tmp_path / "code.php"
    f.write_text("line1\nline2\ntarget\nline4\n")
    out = supertool.op_read(str(f), grep_filter="target")
    assert "3→target" in out


def test_read_grep_filter_no_matches(tmp_path: Path) -> None:
    f = tmp_path / "code.php"
    f.write_text("hello\nworld\n")
    out = supertool.op_read(str(f), grep_filter="ZZZZ")
    assert "no lines matching" in out


def test_read_grep_filter_with_offset(tmp_path: Path) -> None:
    f = tmp_path / "big.txt"
    lines = [f"line{i}\n" for i in range(20)]
    f.write_text("".join(lines))
    out = supertool.op_read(str(f), offset=5, limit=10, grep_filter="line1")
    # only lines 6-15 searched, line10-line14 match "line1"
    assert "line10" in out
    assert "line0" not in out


# ---------------------------------------------------------------------------
# render_file edge cases (shared helper)
# ---------------------------------------------------------------------------

def test_render_file_handles_binary_gracefully(tmp_path: Path) -> None:
    f = tmp_path / "bin.dat"
    f.write_bytes(b"\x00\x01\x02\xff\n")
    out = supertool.render_file(str(f))
    # Should not raise, should emit something
    assert "     1→" in out


def test_render_file_handles_empty_file(tmp_path: Path) -> None:
    f = tmp_path / "empty.txt"
    f.write_text("")
    out = supertool.render_file(str(f))
    assert "(0 lines, 0 bytes)" in out
