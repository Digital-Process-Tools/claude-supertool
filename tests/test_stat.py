from __future__ import annotations

from pathlib import Path

import supertool


# ---------------------------------------------------------------------------
# op_stat
# ---------------------------------------------------------------------------

def test_stat_file(tmp_path: Path) -> None:
    f = tmp_path / "test.txt"
    f.write_text("hello world")
    out = supertool.op_stat(str(f))
    assert "11" in out  # 11 bytes
    assert "file" in out
    assert str(f) in out


def test_stat_directory(tmp_path: Path) -> None:
    d = tmp_path / "subdir"
    d.mkdir()
    out = supertool.op_stat(str(d))
    assert "dir" in out
    assert str(d) in out


def test_stat_not_found(tmp_path: Path) -> None:
    out = supertool.op_stat(str(tmp_path / "nope"))
    assert "ERROR" in out
    assert "not found" in out


def test_stat_empty_path() -> None:
    out = supertool.op_stat("")
    assert "ERROR" in out


def test_stat_dispatch(tmp_path: Path) -> None:
    f = tmp_path / "test.txt"
    f.write_text("data")
    out = supertool.dispatch(f"stat:{f}")
    assert "--- stat:" in out
    assert "file" in out


def test_stat_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("data")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    out = supertool.op_stat(str(link))
    assert "symlink" in out
    assert "-> " in out
    assert "target.txt" in out
    assert "broken" not in out


def test_stat_broken_symlink(tmp_path: Path) -> None:
    link = tmp_path / "dangling.txt"
    link.symlink_to(tmp_path / "missing.txt")
    out = supertool.op_stat(str(link))
    assert "symlink" in out
    assert "(broken)" in out
