"""Tests for validators/git-status/git-status.py."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

GIT_STATUS_PY = Path(__file__).parent.parent / "validators" / "git-status" / "git-status.py"


def _run(file: str = "", env: dict | None = None) -> dict:
    args = [sys.executable, str(GIT_STATUS_PY)]
    if file:
        args.append(file)
    r = subprocess.run(args, capture_output=True, text=True, timeout=10, env=env or os.environ, encoding="utf-8", errors="replace")
    assert r.returncode == 0
    return json.loads(r.stdout.strip())


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, check=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t.com",
             "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t.com"},
    )


def _init_repo(tmp_path: Path) -> Path:
    """Bootstrap a minimal git repo with one committed file."""
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t.com")
    _git(tmp_path, "config", "user.name", "test")
    f = tmp_path / "base.txt"
    f.write_text("line1\nline2\n")
    _git(tmp_path, "add", "base.txt")
    _git(tmp_path, "commit", "-m", "init")
    return tmp_path


def test_no_arg_emits_ok_zero_metrics() -> None:
    """No file arg → ok=true, zero metrics, clean state."""
    data = _run()
    assert data["tool"] == "git-status"
    assert data["ok"] is True
    assert data["count"] == 0
    assert data["errors"] == []
    m = data["metrics"]
    assert m["lines_added"] == 0
    assert m["lines_removed"] == 0
    assert m["state"] == "clean"


def test_clean_file(tmp_path: Path) -> None:
    """Committed, unmodified file → state=clean, zero deltas."""
    repo = _init_repo(tmp_path)
    f = repo / "base.txt"
    data = _run(str(f))
    assert data["ok"] is True
    m = data["metrics"]
    assert m["lines_added"] == 0
    assert m["lines_removed"] == 0
    assert m["state"] == "clean"
    assert isinstance(data["duration_ms"], int)


def test_modified_file(tmp_path: Path) -> None:
    """File with unstaged edits → state=modified, lines_added/removed > 0."""
    repo = _init_repo(tmp_path)
    f = repo / "base.txt"
    f.write_text("line1\nline2\nline3\n")  # +1 line (adds line3)
    data = _run(str(f))
    assert data["ok"] is True
    m = data["metrics"]
    assert m["lines_added"] >= 1
    assert m["state"] == "modified"


def test_staged_file(tmp_path: Path) -> None:
    """File with all changes staged → state=staged, staged delta > 0."""
    repo = _init_repo(tmp_path)
    f = repo / "base.txt"
    f.write_text("line1\nline2\nextra\n")
    _git(repo, "add", "base.txt")
    data = _run(str(f))
    assert data["ok"] is True
    m = data["metrics"]
    assert m["lines_staged_added"] >= 1
    assert m["state"] == "staged"


def test_untracked_file(tmp_path: Path) -> None:
    """New file never added → state=untracked."""
    repo = _init_repo(tmp_path)
    f = repo / "new.txt"
    f.write_text("hello\n")
    data = _run(str(f))
    assert data["ok"] is True
    assert data["metrics"]["state"] == "untracked"


def test_not_in_git_repo(tmp_path: Path) -> None:
    """File outside any git repo → ok=true, zero metrics, state=clean."""
    f = tmp_path / "plain.txt"
    f.write_text("hello\n")
    data = _run(str(f))
    assert data["ok"] is True
    m = data["metrics"]
    assert m["lines_added"] == 0
    assert m["lines_removed"] == 0
    assert m["state"] == "clean"


def test_git_absent(tmp_path: Path) -> None:
    """When the git binary is missing → the third state, not a zeroed metric.

    This asserted `ok is True` alongside a `metrics` block reading
    `state: "clean"` — a measurement of a working tree nothing had looked at,
    and indistinguishable from a file that genuinely had no changes (#1202).
    """
    f = tmp_path / "x.txt"
    f.write_text("x\n")
    env = {**os.environ, "GIT_BIN": "/nonexistent/git"}
    data = _run(str(f), env=env)
    assert "skipped" in data, data
    assert "git not found" in data["skipped"], data
    for key in ("ok", "count", "errors", "metrics"):
        assert key not in data, f"a skip must not carry {key!r}: {data}"
