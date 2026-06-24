"""Tests for the conftest git-state guard (#319).

The guard fingerprints the suite repo's config/HEAD/refs before and after every
test and fails any test that mutates them — the tripwire for a test (or an agent
running the suite in a worktree) corrupting the real repo with `core.bare=true`
or junk commits on master. These tests exercise the fingerprint logic against a
synthetic git layout, never the real repo.
"""
from __future__ import annotations

from pathlib import Path

import conftest


def _fake_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Build a minimal (common_dir, git_dir) layout the fingerprint reads."""
    common = tmp_path / ".git"
    (common / "refs" / "heads").mkdir(parents=True)
    (common / "config").write_text("[core]\n\tbare = false\n")
    (common / "HEAD").write_text("ref: refs/heads/master\n")
    (common / "refs" / "heads" / "master").write_text("a" * 40 + "\n")
    return common, common


def test_fingerprint_stable_when_unchanged(tmp_path: Path) -> None:
    dirs = _fake_repo(tmp_path)
    assert conftest._git_state_fingerprint(dirs) == conftest._git_state_fingerprint(dirs)


def test_fingerprint_detects_core_bare_flip(tmp_path: Path) -> None:
    dirs = _fake_repo(tmp_path)
    before = conftest._git_state_fingerprint(dirs)
    (dirs[0] / "config").write_text("[core]\n\tbare = true\n")
    assert conftest._git_state_fingerprint(dirs) != before


def test_fingerprint_detects_junk_commit_on_a_branch(tmp_path: Path) -> None:
    dirs = _fake_repo(tmp_path)
    before = conftest._git_state_fingerprint(dirs)
    (dirs[0] / "refs" / "heads" / "master").write_text("b" * 40 + "\n")
    assert conftest._git_state_fingerprint(dirs) != before


def test_fingerprint_detects_new_branch_ref(tmp_path: Path) -> None:
    dirs = _fake_repo(tmp_path)
    before = conftest._git_state_fingerprint(dirs)
    (dirs[0] / "refs" / "heads" / "junk").write_text("c" * 40 + "\n")
    assert conftest._git_state_fingerprint(dirs) != before


def test_fingerprint_detects_head_move(tmp_path: Path) -> None:
    dirs = _fake_repo(tmp_path)
    before = conftest._git_state_fingerprint(dirs)
    (dirs[1] / "HEAD").write_text("ref: refs/heads/other\n")
    assert conftest._git_state_fingerprint(dirs) != before


def test_repo_git_dirs_resolves_real_repo() -> None:
    """Sanity: the suite repo is discoverable and its config is readable."""
    dirs = conftest._repo_git_dirs()
    assert dirs is not None
    common_dir, _ = dirs
    assert (common_dir / "config").is_file()
