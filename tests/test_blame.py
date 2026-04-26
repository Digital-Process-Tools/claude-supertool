from __future__ import annotations

import subprocess
from pathlib import Path

import supertool


def _init_git_repo(tmp_path: Path) -> Path:
    """Create a git repo with one committed file, return the file path."""
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(tmp_path), capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(tmp_path), capture_output=True
    )
    f = tmp_path / "code.py"
    f.write_text("line 1\nline 2\nline 3\nline 4\nline 5\nline 6\nline 7\nline 8\nline 9\nline 10\n")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(tmp_path), capture_output=True
    )
    return f


# ---------------------------------------------------------------------------
# op_blame
# ---------------------------------------------------------------------------

def test_blame_basic(tmp_path: Path, monkeypatch) -> None:
    f = _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    out = supertool.op_blame(str(f), 5, 2)
    assert "line 5" in out
    assert "Test" in out or "test@test.com" in out


def test_blame_file_not_found(tmp_path: Path, monkeypatch) -> None:
    _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    out = supertool.op_blame(str(tmp_path / "nope.py"), 1)
    assert "ERROR" in out


def test_blame_not_a_repo(tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / "file.py"
    f.write_text("hello\n")
    monkeypatch.chdir(tmp_path)
    out = supertool.op_blame(str(f), 1)
    assert "ERROR" in out


def test_blame_zero_line(tmp_path: Path, monkeypatch) -> None:
    f = _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    out = supertool.op_blame(str(f), 0)
    assert "ERROR" in out
    assert ">= 1" in out


def test_blame_dispatch(tmp_path: Path, monkeypatch) -> None:
    f = _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    out = supertool.dispatch(f"blame:{f}:5:2")
    assert "--- blame:" in out
    assert "line 5" in out
