from __future__ import annotations

from pathlib import Path

import supertool


# ---------------------------------------------------------------------------
# op_ls
# ---------------------------------------------------------------------------

def test_ls_lists_dir_contents(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("")
    (tmp_path / "sub").mkdir()
    out = supertool.op_ls(str(tmp_path))
    assert "file.txt" in out
    assert "sub/" in out  # trailing slash for dirs


def test_ls_non_existent_path(tmp_path: Path) -> None:
    out = supertool.op_ls(str(tmp_path / "nope"))
    assert "ERROR: not a directory" in out


def test_ls_file_not_dir(tmp_path: Path) -> None:
    f = tmp_path / "file.txt"
    f.write_text("")
    out = supertool.op_ls(str(f))
    assert "ERROR: not a directory" in out


def test_ls_empty_dir(tmp_path: Path) -> None:
    out = supertool.op_ls(str(tmp_path))
    assert "(0 items)" in out
