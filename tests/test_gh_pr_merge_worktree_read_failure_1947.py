"""`_worktrees_for_branch` collapsed a failed read into an empty listing (#1947).

`_cleanup_worktree` read `[]` from `_worktrees_for_branch` as "nobody has this
branch checked out" whether the underlying `git worktree list --porcelain`
genuinely found nothing or the read itself failed. `_worktree_dirt` in the
same module already returns `(value, error)` and this brings the worktree
lookup to the same three-state shape: a failed read is `CLEAN_REFUSED`, never
the same `CLEAN_SKIPPED` a real empty listing gets.
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

MOD_PATH = Path(__file__).parent.parent / "presets" / "github" / "pr_merge.py"
_spec = importlib.util.spec_from_file_location("gh_pr_merge_worktree_read_1947", MOD_PATH)
assert _spec is not None and _spec.loader is not None
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


def test_a_failed_worktree_list_returns_an_error_not_an_empty_list(monkeypatch) -> None:
    """The read function itself now hands back `(paths, error)`."""
    def fake_git(args, timeout=30):
        return subprocess.CompletedProcess(args, 1, "", "fatal: not a git repository")
    monkeypatch.setattr(m, "_git", fake_git)
    paths, err = m._worktrees_for_branch("fix/1947")
    assert paths == []
    assert err != "", "a failed read must say so, not answer like an empty listing"


def test_a_git_that_cannot_be_run_at_all_also_returns_an_error(monkeypatch) -> None:
    def raising_git(args, timeout=30):
        raise FileNotFoundError("no git on PATH")
    monkeypatch.setattr(m, "_git", raising_git)
    paths, err = m._worktrees_for_branch("fix/1947")
    assert paths == []
    assert err != ""


def test_a_genuinely_empty_listing_still_returns_no_error(monkeypatch) -> None:
    """The ordinary negative — paired here so a harness that answers nothing
    at all cannot pass both cases."""
    def fake_git(args, timeout=30):
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(m, "_git", fake_git)
    paths, err = m._worktrees_for_branch("fix/1947")
    assert paths == []
    assert err == ""


def test_a_worktree_holding_the_branch_is_still_found(monkeypatch) -> None:
    porcelain = "worktree /w/fix\nbranch refs/heads/fix/1947\n"
    def fake_git(args, timeout=30):
        return subprocess.CompletedProcess(args, 0, porcelain, "")
    monkeypatch.setattr(m, "_git", fake_git)
    paths, err = m._worktrees_for_branch("fix/1947")
    assert paths == ["/w/fix"]
    assert err == ""


# ---------------------------------------------------------------------------
# `_cleanup_worktree`: a failed read is refused, not skipped
# ---------------------------------------------------------------------------

def test_a_failed_worktree_read_refuses_cleanup_rather_than_skipping_it(
        monkeypatch) -> None:
    monkeypatch.setattr(m, "_worktrees_for_branch",
                         lambda head: ([], "git worktree list exited 1: fatal"))
    item, state, detail = m._cleanup_worktree("fix/1947")
    assert item == "local worktree"
    assert state == m.CLEAN_REFUSED, detail
    assert "fatal" in detail


def test_a_genuinely_empty_read_is_still_a_skip_naming_nothing_was_there(
        monkeypatch) -> None:
    """Paired negative: the ordinary "nobody has it" case must still be
    CLEAN_SKIPPED, not swept into CLEAN_REFUSED by an over-eager fix."""
    monkeypatch.setattr(m, "_worktrees_for_branch", lambda head: ([], ""))
    item, state, detail = m._cleanup_worktree("fix/1947")
    assert item == "local worktree"
    assert state == m.CLEAN_SKIPPED, detail
    assert "no worktree" in detail
