from __future__ import annotations

import os
from pathlib import Path

import supertool


def test_glob_finds_wildcards(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.py").write_text("")
    (tmp_path / "c.txt").write_text("")
    monkeypatch.chdir(tmp_path)
    out = supertool.op_glob("*.py")
    assert "(2 files)" in out
    assert "a.py" in out
    assert "b.py" in out
    assert "c.txt" not in out


def test_glob_recursive_double_star(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep.py").write_text("")
    (tmp_path / "top.py").write_text("")
    monkeypatch.chdir(tmp_path)
    out = supertool.op_glob("**/*.py")
    assert "top.py" in out
    assert os.path.join("sub", "deep.py") in out


def test_glob_empty_pattern_errors() -> None:
    out = supertool.op_glob("")
    assert "ERROR: empty pattern" in out


def test_glob_no_match(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    out = supertool.op_glob("*.nothinglikethis")
    assert "(0 files)" in out


# --- Auto-read on glob (concrete path, no wildcards, is a file) ---

def test_glob_auto_reads_concrete_file(tmp_path: Path) -> None:
    f = tmp_path / "specific.py"
    f.write_text("content = 42\n")
    out = supertool.op_glob(str(f))
    assert "[auto-read: concrete path, no wildcards]" in out
    assert "     1→content = 42" in out


def test_glob_auto_read_when_single_result(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    monkeypatch.chdir(tmp_path)
    out = supertool.op_glob("*.py")
    assert "[auto-read: glob returned 1 file]" in out
    assert "x = 1" in out


def test_glob_no_auto_read_when_multiple_results(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("y = 2\n")
    monkeypatch.chdir(tmp_path)
    out = supertool.op_glob("*.py")
    assert "[auto-read:" not in out


def test_glob_no_auto_read_on_concrete_directory(tmp_path: Path) -> None:
    # Directory path with no wildcards — glob() returns the dir name, but
    # we should NOT auto-read (it's not a file).
    out = supertool.op_glob(str(tmp_path))
    assert "[auto-read:" not in out


def test_glob_no_auto_read_on_missing_concrete_file(tmp_path: Path) -> None:
    out = supertool.op_glob(str(tmp_path / "nope.py"))
    assert "[auto-read:" not in out
    assert "(0 files)" in out


def test_glob_question_mark_is_wildcard(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.py").write_text("")
    monkeypatch.chdir(tmp_path)
    out = supertool.op_glob("?.py")
    # Question-mark means single-char wildcard — should NOT auto-read even
    # if it could match one file.
    assert "[auto-read:" not in out
    assert "a.py" in out
