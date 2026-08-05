"""Unit tests for presets/git/merge.py — conflict block helpers."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


PRESET = Path(__file__).parent.parent / "presets" / "git" / "merge.py"
_spec = importlib.util.spec_from_file_location("git_merge", PRESET)
assert _spec is not None and _spec.loader is not None
merge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(merge)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + list(args), cwd=repo, capture_output=True, text=True, check=True, encoding="utf-8", errors="replace"
    )


def _make_clones_with_stale_master(tmp_path: Path) -> tuple[Path, Path]:
    """Bare remote + two clones. Advance master via clone B, push.

    Clone A's local master is now stale (behind origin/master). Returns
    (clone_a, remote). A feature branch 'feat' exists in clone A, branched
    from the original master, so merging 'master' into it should pull the
    new commit only if the fetch+staleness redirect works.
    """
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-b", "master", str(remote))

    a = tmp_path / "a"
    _git(tmp_path, "clone", str(remote), str(a))
    _git(a, "config", "user.email", "a@test.com")
    _git(a, "config", "user.name", "A")
    (a / "README.md").write_text("hello\n")
    _git(a, "add", "README.md")
    _git(a, "commit", "-m", "init")
    _git(a, "push", "origin", "master")
    # feature branch off the original master
    _git(a, "checkout", "-b", "feat")
    (a / "feat.txt").write_text("feat\n")
    _git(a, "add", "feat.txt")
    _git(a, "commit", "-m", "feat work")

    # Clone B advances master and pushes — origin/master is now ahead.
    b = tmp_path / "b"
    _git(tmp_path, "clone", str(remote), str(b))
    _git(b, "config", "user.email", "b@test.com")
    _git(b, "config", "user.name", "B")
    (b / "fix.txt").write_text("the fix\n")
    _git(b, "add", "fix.txt")
    _git(b, "commit", "-m", "the fix that wasnt there")
    _git(b, "push", "origin", "master")

    return a, remote


def test_merge_redirects_to_upstream_when_local_stale(tmp_path, monkeypatch, capsys):
    a, _ = _make_clones_with_stale_master(tmp_path)
    monkeypatch.chdir(a)
    # On 'feat', local 'master' is stale; origin/master has fix.txt.
    monkeypatch.setattr(sys, "argv", ["merge.py", "master"])
    rc = merge.main()
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "behind origin/master" in out
    assert "merging origin/master" in out
    # The fix commit must now be present on feat.
    assert (a / "fix.txt").exists(), "stale merge missed the upstream fix"


def test_merge_local_only_branch_no_fetch(tmp_path, monkeypatch, capsys):
    """A branch with no upstream merges as-is (offline / local-only safe)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "master")
    _git(repo, "config", "user.email", "t@test.com")
    _git(repo, "config", "user.name", "T")
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "init")
    _git(repo, "checkout", "-b", "side")
    (repo / "b.txt").write_text("b\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-m", "side work")
    _git(repo, "checkout", "master")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(sys, "argv", ["merge.py", "side"])
    rc = merge.main()
    out = capsys.readouterr().out
    assert rc == 0, out
    assert (repo / "b.txt").exists()


def test_first_block_extracts_markers(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text(
        "before\n"
        "<<<<<<< HEAD\n"
        "ours\n"
        "=======\n"
        "theirs\n"
        ">>>>>>> branch\n"
        "after\n"
    )
    out = merge._first_conflict_block(str(f), max_lines=20)
    assert "<<<<<<< HEAD" in out
    assert "ours" in out
    assert "theirs" in out
    assert ">>>>>>> branch" in out
    assert "L2:" in out  # marker line numbers preserved


def test_first_block_truncates_long_block(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    body = "\n".join(f"line{i}" for i in range(50))
    f.write_text(f"<<<<<<< HEAD\n{body}\n=======\nx\n>>>>>>> branch\n")
    out = merge._first_conflict_block(str(f), max_lines=5)
    assert "truncated at 5" in out


def test_count_blocks(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text(
        "<<<<<<< HEAD\na\n=======\nb\n>>>>>>> br\n"
        "<<<<<<< HEAD\nc\n=======\nd\n>>>>>>> br\n"
    )
    assert merge._count_blocks(str(f)) == 2


def test_no_markers_message(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("plain\n")
    out = merge._first_conflict_block(str(f), max_lines=10)
    assert "no <<<<<<< marker" in out
