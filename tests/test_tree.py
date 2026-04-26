from __future__ import annotations

from pathlib import Path

import supertool


# ---------------------------------------------------------------------------
# op_tree
# ---------------------------------------------------------------------------

def test_tree_basic(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("hello")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.py").write_text("code")
    out = supertool.op_tree(str(tmp_path), 2)
    assert "file.txt" in out
    assert "sub/" in out
    assert "nested.py" in out


def test_tree_depth_limit(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b").mkdir()
    (tmp_path / "a" / "b" / "c").mkdir()
    (tmp_path / "a" / "b" / "c" / "deep.txt").write_text("deep")
    out = supertool.op_tree(str(tmp_path), 1)
    assert "a/" in out
    assert "b/" not in out
    assert "deep.txt" not in out


def test_tree_depth_2_shows_two_levels(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b").mkdir()
    (tmp_path / "a" / "b" / "c").mkdir()
    out = supertool.op_tree(str(tmp_path), 2)
    assert "a/" in out
    assert "b/" in out
    assert "c/" not in out


def test_tree_hides_dotfiles(tmp_path: Path) -> None:
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "visible.txt").write_text("yes")
    out = supertool.op_tree(str(tmp_path))
    assert "visible.txt" in out
    assert ".hidden" not in out


def test_tree_not_a_directory(tmp_path: Path) -> None:
    f = tmp_path / "file.txt"
    f.write_text("hello")
    out = supertool.op_tree(str(f))
    assert "ERROR" in out
    assert "not a directory" in out


def test_tree_empty_path_uses_cwd(tmp_path: Path, monkeypatch: object) -> None:
    import os
    monkeypatch.chdir(tmp_path)  # type: ignore[attr-defined]
    (tmp_path / "here.txt").write_text("yes")
    out = supertool.op_tree("")
    assert "here.txt" in out


def test_tree_zero_depth() -> None:
    out = supertool.op_tree(".", 0)
    assert "ERROR" in out
    assert ">= 1" in out


def test_tree_dispatch(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "sub").mkdir()
    out = supertool.dispatch(f"tree:{tmp_path}:2")
    assert "--- tree:" in out
    assert "a.txt" in out
    assert "sub/" in out


def test_tree_default_depth(tmp_path: Path) -> None:
    """Default depth is 3."""
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b").mkdir()
    (tmp_path / "a" / "b" / "c").mkdir()
    (tmp_path / "a" / "b" / "c" / "d").mkdir()
    out = supertool.op_tree(str(tmp_path))
    assert "c/" in out
    assert "d/" not in out  # depth 4, default is 3
