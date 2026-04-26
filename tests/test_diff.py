from __future__ import annotations

from pathlib import Path

import supertool


# ---------------------------------------------------------------------------
# op_diff
# ---------------------------------------------------------------------------

def test_diff_identical_files(tmp_path: Path) -> None:
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.txt"
    f1.write_text("hello\nworld\n")
    f2.write_text("hello\nworld\n")
    out = supertool.op_diff(str(f1), str(f2))
    assert "identical" in out


def test_diff_different_files(tmp_path: Path) -> None:
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.txt"
    f1.write_text("hello\nworld\n")
    f2.write_text("hello\nearth\n")
    out = supertool.op_diff(str(f1), str(f2))
    assert "---" in out
    assert "+++" in out
    assert "-world" in out
    assert "+earth" in out


def test_diff_file_not_found(tmp_path: Path) -> None:
    f1 = tmp_path / "exists.txt"
    f1.write_text("hello")
    out = supertool.op_diff(str(f1), str(tmp_path / "nope.txt"))
    assert "ERROR" in out
    assert "not found" in out


def test_diff_empty_path() -> None:
    out = supertool.op_diff("", "some/file.txt")
    assert "ERROR" in out


def test_diff_empty_files(tmp_path: Path) -> None:
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.txt"
    f1.write_text("")
    f2.write_text("")
    out = supertool.op_diff(str(f1), str(f2))
    assert "identical" in out


def test_diff_dispatch(tmp_path: Path) -> None:
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.txt"
    f1.write_text("line1\n")
    f2.write_text("line2\n")
    out = supertool.dispatch(f"diff:{f1}:{f2}")
    assert "--- diff:" in out
    assert "-line1" in out
    assert "+line2" in out
