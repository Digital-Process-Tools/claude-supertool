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
