"""#1016 — a green tick over a partial commit says nothing was left out.

`git-commit:::MSG:::PATHS` printed

    PASS ✓
    7 files changed
    Files committed: 5

with two modified tracked files still sitting unstaged. The op already read
the working tree — that is where the numbers came from — so this is not a
limitation, it is a render that stops one line short, under a tick that
argues nothing is missing.

Three states, on the write side:

  - nothing left behind      → silent (the common case gains no line)
  - something left behind    → named, not counted
  - could not tell           → said so, never rendered as "nothing left"

Untracked files are deliberately *not* listed. Almost every worktree has
some, and a list on every commit is a list nobody reads on the commit that
needed it — they are counted in the same line and no more. A **modified
tracked** file left behind is the one that means "you edited this and did
not commit it".
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
SUPERTOOL = REPO / "supertool.py"
COAUTHOR = "Test Bot <bot@example.invalid>"

_COMMIT_PATH = REPO / "presets" / "git" / "commit.py"
_spec = importlib.util.spec_from_file_location("git_commit_1016", _COMMIT_PATH)
assert _spec is not None and _spec.loader is not None
commit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(commit)


def _repo(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t"),
                 ("commit.gpgsign", "false")):
        subprocess.run(["git", "config", k, v], cwd=work, check=True)
    (work / ".supertool.json").write_text('{"presets": ["git"]}\n', encoding="utf-8")
    (work / "sub").mkdir()
    for name in ("a.txt", "b.txt", "c.txt", "sub/d.txt", "sub/e.txt"):
        (work / name).write_text("1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=work, check=True)
    return work


def _run(args: list[str], cwd: Path) -> str:
    env = dict(os.environ)
    env["SUPERTOOL_COAUTHOR"] = COAUTHOR
    proc = subprocess.run(
        [sys.executable, str(SUPERTOOL), *args],
        capture_output=True, text=True, timeout=120,
        encoding="utf-8", errors="replace", cwd=str(cwd), env=env,
    )
    return proc.stdout + proc.stderr


def test_a_partial_commit_names_what_it_left_behind(tmp_path: Path) -> None:
    work = _repo(tmp_path)
    for name in ("a.txt", "b.txt", "c.txt", "sub/d.txt", "sub/e.txt"):
        (work / name).write_text("2\n", encoding="utf-8")

    out = _run(["git-commit:::subject:::a.txt:::b.txt:::c.txt"], cwd=work)

    assert "Files committed: 3" in out, out
    # Named, not counted — "2 not included" costs a second call to find out
    # which, and a reader who has to make that call usually does not.
    assert "sub/d.txt" in out, out
    assert "sub/e.txt" in out, out
    # And the reader is told this is a question, not a failure.
    assert "NOT included" in out or "not included" in out, out


def test_a_complete_commit_gains_no_warning(tmp_path: Path) -> None:
    """The receipt must not become noise on the run that left nothing out."""
    work = _repo(tmp_path)
    (work / "a.txt").write_text("2\n", encoding="utf-8")
    out = _run(["git-commit:::subject:::a.txt"], cwd=work)
    assert "Files committed: 1" in out, out
    assert "not included" not in out.lower(), out


def test_untracked_files_are_counted_but_not_listed(tmp_path: Path) -> None:
    """A worktree full of scratch files must not print a scratch-file list."""
    work = _repo(tmp_path)
    (work / "a.txt").write_text("2\n", encoding="utf-8")
    (work / "b.txt").write_text("2\n", encoding="utf-8")
    (work / "scratch1.tmp").write_text("x\n", encoding="utf-8")
    (work / "scratch2.tmp").write_text("x\n", encoding="utf-8")

    out = _run(["git-commit:::subject:::a.txt"], cwd=work)

    assert "b.txt" in out, out
    assert "scratch1.tmp" not in out, out
    assert "2 untracked" in out, out


def test_a_deleted_tracked_file_left_behind_is_named(tmp_path: Path) -> None:
    """A deletion is a change left uncommitted just as much as an edit."""
    work = _repo(tmp_path)
    (work / "a.txt").write_text("2\n", encoding="utf-8")
    (work / "b.txt").unlink()
    out = _run(["git-commit:::subject:::a.txt"], cwd=work)
    assert "b.txt" in out, out


# --- The third state: a check that could not run must not read as "clean" ---


def test_a_left_behind_check_that_cannot_run_says_so_not_nothing() -> None:
    """An unanswered `git status` must produce a decline, never silence.

    Silence is byte-for-byte the render of "nothing was left behind", which
    is the defect this whole fix exists to remove — one layer along.
    """
    def dead(args, timeout=None):
        return subprocess.CompletedProcess(
            args=["git"] + list(args), returncode=128, stdout="",
            stderr="fatal: unable to read index",
        )

    lines = commit._left_behind_lines(dead)
    assert lines, "an unanswered check rendered as nothing at all"
    joined = "\n".join(lines)
    assert "SKIPPED" in joined or "UNKNOWN" in joined, joined
    assert "unable to read index" in joined, joined


def test_a_clean_answered_check_renders_nothing() -> None:
    def clean(args, timeout=None):
        return subprocess.CompletedProcess(
            args=["git"] + list(args), returncode=0, stdout="", stderr="",
        )

    assert commit._left_behind_lines(clean) == []


def test_paths_with_spaces_and_non_ascii_survive_the_check() -> None:
    """`git status --porcelain` quotes such paths; `-z` does not.

    Asserted against the raw record stream rather than a shell, so it runs
    identically on every platform.
    """
    def porcelain(args, timeout=None):
        return subprocess.CompletedProcess(
            args=["git"] + list(args), returncode=0,
            stdout=" M sub/a file.txt\0 M sub/café.txt\0?? junk\0", stderr="",
        )

    joined = "\n".join(commit._left_behind_lines(porcelain))
    assert "sub/a file.txt" in joined
    assert "sub/café.txt" in joined
    assert "junk" not in joined
    assert "1 untracked" in joined


def test_a_staged_rename_does_not_swallow_the_next_record() -> None:
    """`-z` emits a rename as two records; the second is the source path and
    must not be parsed as a status line of its own."""
    def porcelain(args, timeout=None):
        return subprocess.CompletedProcess(
            args=["git"] + list(args), returncode=0,
            stdout="R  new.txt\0old.txt\0 M left.txt\0", stderr="",
        )

    joined = "\n".join(commit._left_behind_lines(porcelain))
    assert "left.txt" in joined
    assert "old.txt" not in joined
