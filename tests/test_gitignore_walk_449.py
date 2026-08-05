"""#449 — glob/grep must not descend into gitignored directories.

The reported case is this repository's own workflow: Claude Code creates a git
worktree per agent under `.claude/worktrees/`, which is gitignored, so every
`glob:**/Foo.php` returned one hit per worktree plus one real one — 6 of 7
results noise — and every `grep` paid for the same tree N times over. The
`scanned N files` denominator #407/#414 exist to make meaningful was counting
the same files repeatedly.

The pins here are deliberately about *behaviour at the walk boundary*, not
about `.gitignore` parsing: supertool asks git which directories are ignored
(`git ls-files --others --ignored --directory`) rather than reimplementing
pattern semantics. The two pins that matter most are the last two — an ignored
tree named explicitly as the search root must still be searched (silence there
would be a new instance of the defect class this repo has been removing), and
the reduced walk must show up in the denominator rather than being hidden.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

import supertool

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not installed"
)

GIT = ["git", "-c", "user.email=f@example.invalid", "-c", "user.name=f"]


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(GIT + args, cwd=str(cwd), capture_output=True, text=True, check=False, encoding="utf-8", errors="replace")


# `_GIT_IGNORED_CACHE` is per-run scratch and is listed in conftest's
# RESET_GLOBALS, so it starts empty for every test here. The explicit clears
# below are the mid-test ones — a cache already warmed inside the same test,
# before the setting under test was changed.


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repo with one tracked match and one match inside a gitignored tree.

    Mirrors the reported shape, including the ordering that made it dangerous:
    `ignored_tree` sorts before `src`, so the stale copy was the hit an agent
    read first.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("needle here\n")

    deep = tmp_path / "ignored_tree" / "deep"
    deep.mkdir(parents=True)
    (deep / "copy.py").write_text("needle here\n")

    (tmp_path / ".gitignore").write_text("ignored_tree/\n")

    _git(["init", "-q"], tmp_path)
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-q", "-m", "init"], tmp_path)

    monkeypatch.chdir(tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# The ignore set itself
# ---------------------------------------------------------------------------


def test_git_ignored_dirs_reports_the_ignored_tree(repo: Path) -> None:
    """Guard on every other test here: if git never answers, they pin nothing."""
    assert supertool._git_ignored_dirs(".") == frozenset({"ignored_tree"})


def test_git_ignored_dirs_is_empty_outside_a_repo(tmp_path: Path, monkeypatch) -> None:
    """No repo, no opinion — the walk must be unchanged, not empty."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("needle here\n")
    monkeypatch.chdir(tmp_path)
    assert supertool._git_ignored_dirs(".") == frozenset()


# ---------------------------------------------------------------------------
# Pin 3 — skipped by default from the repo root
# ---------------------------------------------------------------------------


def test_glob_skips_a_gitignored_directory(repo: Path) -> None:
    out = supertool.op_glob("**/*.py", no_auto_read=True)
    assert "src/app.py" in out.replace(os.sep, "/")
    assert "ignored_tree" not in out
    assert "(1 files)" in out


def test_grep_skips_a_gitignored_directory(repo: Path) -> None:
    out = supertool.op_grep("needle", ".", no_auto_read=True)
    assert "src/app.py" in out.replace(os.sep, "/")
    assert "ignored_tree" not in out
    assert "(1 results in 1 files" in out


# ---------------------------------------------------------------------------
# Pin 4 — still searchable when named explicitly
# ---------------------------------------------------------------------------


def test_grep_searches_a_gitignored_path_named_as_the_root(repo: Path) -> None:
    """A user who types the ignored path meant it. Silence here would be the
    same defect one layer down."""
    out = supertool.op_grep("needle", "ignored_tree", no_auto_read=True)
    assert "copy.py" in out
    assert "(1 results in 1 files" in out


def test_glob_searches_a_gitignored_path_named_as_the_root(repo: Path) -> None:
    out = supertool.op_glob("ignored_tree/**/*.py", no_auto_read=True)
    assert "copy.py" in out
    assert "(1 files)" in out


def test_grep_no_exclude_reaches_the_ignored_tree(repo: Path) -> None:
    """`no-exclude` is the documented per-call escape hatch and must cover the
    gitignore prune too, not just the built-in exclude list."""
    out = supertool.op_grep("needle", ".", no_exclude=True, no_auto_read=True)
    assert "ignored_tree" in out


def test_gitignore_pruning_can_be_switched_off_by_config(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(supertool, "_CONFIG", {"gitignore": False})
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    supertool._GIT_IGNORED_CACHE.clear()
    out = supertool.op_grep("needle", ".", no_auto_read=True)
    assert "ignored_tree" in out


# ---------------------------------------------------------------------------
# Pin 5 — the denominator reflects the reduced walk
# ---------------------------------------------------------------------------


def test_scanned_denominator_reflects_the_reduced_walk(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#407's number stays honest: it must count what was actually opened, so
    pruning has to shrink it rather than leaving the old inflated figure."""
    out = supertool.op_grep("needle", ".", no_auto_read=True)
    # .gitignore + src/app.py — ignored_tree/deep/copy.py is never reached.
    assert ", scanned 2 files," in out

    # Same call with only the gitignore prune switched off, so the delta is
    # attributable to this change and not to the built-in exclude list.
    monkeypatch.setenv("SUPERTOOL_NO_GITIGNORE", "1")
    supertool._GIT_IGNORED_CACHE.clear()
    unpruned = supertool.op_grep("needle", ".", no_auto_read=True)
    assert ", scanned 3 files," in unpruned


# ---------------------------------------------------------------------------
# Backend agreement — the delegated path must not disagree with the walker
# ---------------------------------------------------------------------------


def test_rtk_delegation_is_skipped_when_an_ignored_tree_would_be_walked(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rtk shells out to system grep, whose --exclude-dir cannot express a
    nested ignored path — so a delegated grep would return the worktree copies
    the native walker prunes. Results must not depend on which backend ran."""
    calls: list[list[str]] = []

    def _stub(args, timeout: int = 30):
        calls.append(list(args))
        return "unused\n"

    monkeypatch.setattr(supertool, "_CONFIG", {"rtk": True})
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    monkeypatch.setattr(supertool, "_RTK_CHECKED", True)
    monkeypatch.setattr(supertool, "_RTK_PATH", "/fake/bin/rtk")
    monkeypatch.setattr(supertool, "_rtk_run", _stub)
    supertool._GIT_IGNORED_CACHE.clear()

    out = supertool.op_grep("needle", ".", no_auto_read=True)
    assert not calls, "delegated to rtk despite an ignored tree the walker prunes"
    assert "ignored_tree" not in out


def test_rtk_delegation_survives_when_nothing_extra_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate is residual-only: an ignored tree the built-in excludes already
    prune (node_modules/) must not cost anyone their rtk delegation."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("needle here\n")
    nm = tmp_path / "node_modules" / "lodash"
    nm.mkdir(parents=True)
    (nm / "index.js").write_text("needle here\n")
    (tmp_path / ".gitignore").write_text("node_modules/\n")
    _git(["init", "-q"], tmp_path)
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-q", "-m", "init"], tmp_path)
    monkeypatch.chdir(tmp_path)

    calls: list[list[str]] = []

    def _stub(args, timeout: int = 30):
        calls.append(list(args))
        return f"{os.path.join('.', 'src', 'app.py')}:1:needle here\n"

    monkeypatch.setattr(supertool, "_CONFIG", {"rtk": True})
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    monkeypatch.setattr(supertool, "_RTK_CHECKED", True)
    monkeypatch.setattr(supertool, "_RTK_PATH", "/fake/bin/rtk")
    monkeypatch.setattr(supertool, "_rtk_run", _stub)
    supertool._GIT_IGNORED_CACHE.clear()

    supertool.op_grep("needle", ".", no_auto_read=True)
    assert calls, "delegation lost for an ignore set the excludes already cover"
