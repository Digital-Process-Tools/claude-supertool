"""Tests for presets/git/status.py — base-branch divergence + MR diff size.

Uses real git in tmp_path because status.py calls many subprocess endpoints.
Stubs out glab/gh (network).
"""
from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

PRESET_PATH = Path(__file__).parent.parent / "presets" / "git" / "status.py"
_spec = importlib.util.spec_from_file_location("git_status", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
status = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(status)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True,
    )


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "master")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "f").write_text("x\n")
    _git(repo, "add", "f")
    _git(repo, "commit", "-m", "initial")
    return repo


def _stub_no_mr(monkeypatch) -> None:
    """Block glab/gh subprocess calls — no MR found."""
    real_run = subprocess.run

    def fake_run(args, *a, **kw):
        if args and args[0] in ("glab", "gh"):
            return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="")
        return real_run(args, *a, **kw)

    monkeypatch.setattr(status.subprocess, "run", fake_run)


def _run_main(repo: Path, monkeypatch) -> str:
    monkeypatch.chdir(repo)
    buf = io.StringIO()
    with redirect_stdout(buf):
        status.main()
    return buf.getvalue()


def test_branch_with_zero_commits_warns(tmp_path: Path, monkeypatch) -> None:
    repo = _init_repo(tmp_path)
    _git(repo, "checkout", "-b", "feature")  # branched at master tip, no commits
    _stub_no_mr(monkeypatch)
    out = _run_main(repo, monkeypatch)
    assert "0 ahead" in out
    assert "no own commits" in out


def test_branch_with_commits_shows_count(tmp_path: Path, monkeypatch) -> None:
    repo = _init_repo(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    (repo / "g").write_text("y\n")
    _git(repo, "add", "g")
    _git(repo, "commit", "-m", "feature work")
    _stub_no_mr(monkeypatch)
    out = _run_main(repo, monkeypatch)
    assert "vs master: 1 ahead" in out
    assert "no own commits" not in out


def test_branch_behind_master_shows_behind_count(tmp_path: Path, monkeypatch) -> None:
    repo = _init_repo(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    _git(repo, "checkout", "master")
    (repo / "h").write_text("z\n")
    _git(repo, "add", "h")
    _git(repo, "commit", "-m", "advance master")
    _git(repo, "checkout", "feature")
    _stub_no_mr(monkeypatch)
    out = _run_main(repo, monkeypatch)
    assert "0 ahead" in out
    assert "1 behind" in out


def test_on_master_no_divergence_line(tmp_path: Path, monkeypatch) -> None:
    repo = _init_repo(tmp_path)
    _stub_no_mr(monkeypatch)
    out = _run_main(repo, monkeypatch)
    assert "vs master" not in out  # comparing master to itself: skip


def test_main_branch_used_when_master_missing(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "main_repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "T")
    (repo / "f").write_text("x\n")
    _git(repo, "add", "f")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "checkout", "-b", "feature")
    _stub_no_mr(monkeypatch)
    out = _run_main(repo, monkeypatch)
    assert "vs main: 0 ahead" in out
