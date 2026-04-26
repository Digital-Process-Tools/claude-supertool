from __future__ import annotations

from pathlib import Path

import supertool


# ---------------------------------------------------------------------------
# op_around_line
# ---------------------------------------------------------------------------

def test_around_line_basic(tmp_path: Path) -> None:
    f = tmp_path / "test.py"
    f.write_text("".join(f"line {i}\n" for i in range(1, 21)))
    out = supertool.op_around_line(str(f), 10, 3)
    assert "→" in out
    assert "line 10" in out
    assert "line 7" in out   # 3 lines before
    assert "line 13" in out  # 3 lines after


def test_around_line_first_line(tmp_path: Path) -> None:
    f = tmp_path / "test.py"
    f.write_text("first\nsecond\nthird\n")
    out = supertool.op_around_line(str(f), 1, 2)
    assert "→" in out
    assert "first" in out


def test_around_line_last_line(tmp_path: Path) -> None:
    f = tmp_path / "test.py"
    f.write_text("a\nb\nc\n")
    out = supertool.op_around_line(str(f), 3, 2)
    assert "→" in out
    assert "c" in out


def test_around_line_file_not_found() -> None:
    out = supertool.op_around_line("/nonexistent/file.py", 5)
    assert "ERROR" in out
    assert "not found" in out


def test_around_line_exceeds_length(tmp_path: Path) -> None:
    f = tmp_path / "short.py"
    f.write_text("one\ntwo\n")
    out = supertool.op_around_line(str(f), 99)
    assert "ERROR" in out
    assert "exceeds" in out


def test_around_line_zero_line(tmp_path: Path) -> None:
    f = tmp_path / "test.py"
    f.write_text("hello\n")
    out = supertool.op_around_line(str(f), 0)
    assert "ERROR" in out
    assert ">= 1" in out


def test_around_line_dispatch(tmp_path: Path) -> None:
    f = tmp_path / "test.py"
    f.write_text("alpha\nbeta\ngamma\ndelta\n")
    out = supertool.dispatch(f"around_line:{f}:2:1")
    assert "--- around_line:" in out
    assert "→" in out
    assert "beta" in out
