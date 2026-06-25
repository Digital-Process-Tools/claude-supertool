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


def _run_main(repo: Path, monkeypatch, *args: str) -> str:
    monkeypatch.chdir(repo)
    monkeypatch.setattr(status.sys, "argv", ["status.py", *args])
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


def test_origin_head_shown_when_upstream_set(tmp_path: Path, monkeypatch) -> None:
    """When branch tracks an upstream, git-status surfaces origin HEAD sha + subject."""
    upstream = tmp_path / "upstream.git"
    upstream.mkdir()
    _git(upstream, "init", "--bare", "-b", "master")

    repo = _init_repo(tmp_path)
    _git(repo, "remote", "add", "origin", str(upstream))
    _git(repo, "push", "-u", "origin", "master")

    # Local advances by one commit; origin still at the initial commit.
    (repo / "g").write_text("y\n")
    _git(repo, "add", "g")
    _git(repo, "commit", "-m", "local advance")

    _stub_no_mr(monkeypatch)
    out = _run_main(repo, monkeypatch)
    assert "Origin HEAD:" in out
    # The Origin HEAD line should describe the upstream commit, not the local one.
    origin_line = next(l for l in out.splitlines() if l.startswith("Origin HEAD:"))
    assert "initial" in origin_line
    assert "local advance" not in origin_line


def test_origin_head_skipped_when_no_upstream(tmp_path: Path, monkeypatch) -> None:
    """No upstream tracking → no Origin HEAD line (silent skip, not an error)."""
    repo = _init_repo(tmp_path)
    _stub_no_mr(monkeypatch)
    out = _run_main(repo, monkeypatch)
    assert "Origin HEAD:" not in out


def test_other_branch_unpushed_commit_surfaced(tmp_path: Path, monkeypatch) -> None:
    """Commit on a branch you're NOT standing on is surfaced, not hidden.

    The exact failure that motivated this: work committed to master, then a
    feature branch checked out — from `feature` the work looked lost.
    """
    upstream = tmp_path / "upstream.git"
    upstream.mkdir()
    _git(upstream, "init", "--bare", "-b", "master")

    repo = _init_repo(tmp_path)
    _git(repo, "remote", "add", "origin", str(upstream))
    _git(repo, "push", "-u", "origin", "master")

    # Commit on master (now ahead of origin/master), then leave it behind.
    (repo / "g").write_text("y\n")
    _git(repo, "add", "g")
    _git(repo, "commit", "-m", "work that lands on master")
    _git(repo, "checkout", "-b", "feature")

    _stub_no_mr(monkeypatch)
    out = _run_main(repo, monkeypatch)
    assert "Other branches with unpushed/unpulled work" in out
    # The master line must be present and bracket-free, matching the clean
    # `ahead N, behind N` style the rest of the file prints.
    master_line = next(l for l in out.splitlines()
                       if l.strip().startswith("master"))
    assert master_line.strip() == "master  ahead 1"
    assert "[ahead" not in out


def test_untracked_truncated_by_default(tmp_path: Path, monkeypatch) -> None:
    """Default view caps untracked at 10 and prints a '... (N more)' marker."""
    repo = _init_repo(tmp_path)
    for i in range(15):
        (repo / f"untracked_{i:02d}").write_text("x\n")
    _stub_no_mr(monkeypatch)
    out = _run_main(repo, monkeypatch)
    assert "### Untracked (15)" in out
    assert "... (5 more)" in out
    assert "untracked_14" not in out


def test_full_mode_lists_every_untracked_file(tmp_path: Path, monkeypatch) -> None:
    """`git-status:full` drops the cap — every path listed, no truncation marker."""
    repo = _init_repo(tmp_path)
    for i in range(15):
        (repo / f"untracked_{i:02d}").write_text("x\n")
    _stub_no_mr(monkeypatch)
    out = _run_main(repo, monkeypatch, "full")
    assert "### Untracked (15)" in out
    assert "more)" not in out
    assert "untracked_00" in out
    assert "untracked_14" in out


def test_porcelain_alias_also_uncaps(tmp_path: Path, monkeypatch) -> None:
    """`:porcelain` is an accepted alias for `:full`."""
    repo = _init_repo(tmp_path)
    for i in range(15):
        (repo / f"untracked_{i:02d}").write_text("x\n")
    _stub_no_mr(monkeypatch)
    out = _run_main(repo, monkeypatch, "porcelain")
    assert "untracked_14" in out
    assert "more)" not in out


def test_gone_branch_filtered_from_section(tmp_path: Path, monkeypatch) -> None:
    """A branch whose upstream was deleted ([gone]) is stale, not work — skipped."""
    upstream = tmp_path / "upstream_gone.git"
    upstream.mkdir()
    _git(upstream, "init", "--bare", "-b", "master")

    repo = _init_repo(tmp_path)
    _git(repo, "remote", "add", "origin", str(upstream))
    _git(repo, "push", "-u", "origin", "master")

    # A branch that tracks an upstream which then disappears → [gone].
    _git(repo, "checkout", "-b", "doomed")
    _git(repo, "push", "-u", "origin", "doomed")
    _git(repo, "push", "origin", "--delete", "doomed")
    _git(repo, "fetch", "--prune")
    _git(repo, "checkout", "master")
    _git(repo, "checkout", "-b", "feature")

    _stub_no_mr(monkeypatch)
    out = _run_main(repo, monkeypatch)
    assert "doomed" not in out


def test_detached_head_still_lists_diverging_branches(tmp_path: Path, monkeypatch) -> None:
    """Detached HEAD has no current branch — all diverging branches are 'other'
    and must still be surfaced (no orienting branch = need the overview most)."""
    upstream = tmp_path / "upstream_det.git"
    upstream.mkdir()
    _git(upstream, "init", "--bare", "-b", "master")

    repo = _init_repo(tmp_path)
    _git(repo, "remote", "add", "origin", str(upstream))
    _git(repo, "push", "-u", "origin", "master")
    (repo / "g").write_text("y\n")
    _git(repo, "add", "g")
    _git(repo, "commit", "-m", "ahead on master")
    _git(repo, "checkout", "--detach", "HEAD")

    _stub_no_mr(monkeypatch)
    out = _run_main(repo, monkeypatch)
    assert "Other branches with unpushed/unpulled work" in out
    assert "master" in out


def test_no_other_branches_section_when_all_in_sync(tmp_path: Path, monkeypatch) -> None:
    """Section is omitted entirely when no other branch has divergent work."""
    upstream = tmp_path / "upstream2.git"
    upstream.mkdir()
    _git(upstream, "init", "--bare", "-b", "master")

    repo = _init_repo(tmp_path)
    _git(repo, "remote", "add", "origin", str(upstream))
    _git(repo, "push", "-u", "origin", "master")
    _git(repo, "checkout", "-b", "feature")

    _stub_no_mr(monkeypatch)
    out = _run_main(repo, monkeypatch)
    assert "Other branches with unpushed/unpulled work" not in out
