"""#1228 — `git-commit` committed the whole index, not the paths it was given.

`git add <paths>` followed by a **pathspec-less** `git commit` means anything
another process left staged rides into the commit. On 2026-08-09 a review agent
left a revert staged; the next `git-commit` swept it up and silently un-did 139
lines of a production fix while the worktree still held the correct file and the
tests still passed against the worktree.

A commit that removes the fix and keeps the tests green is the worst shape
available: nothing downstream can catch it.

Two halves are asserted here, and they are not the same claim:

1. **The commit is scoped.** `git-commit:::MSG:::A` commits `A` and nothing
   else, whatever else is in the index.
2. **The receipt says what stayed behind.** A path that was staged and is *not*
   in the commit is named. Silence there is byte-for-byte the receipt of a
   complete commit, which is how the original defect went unread.

And three things that must NOT change:

* the pathless call (`git-commit:::MSG`) still commits the index — that is the
  deliberate "commit what I staged by hand" route, and it is the answer to
  whether the whole-index behaviour was ever wanted;
* a staged deletion named as a PATH is still committed (#324);
* a commit during a conflicted merge still works — `git commit -- <path>` is a
  partial commit and git refuses those outright during a merge (`fatal: cannot
  do a partial commit during a merge`), so the pathspec must be dropped there.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _changelog_findable import assert_change_is_findable  # noqa: E402

REPO = Path(__file__).parent.parent
SUPERTOOL = REPO / "supertool.py"
COAUTHOR = "Test Bot <bot@example.invalid>"


def _repo(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t"),
                 ("commit.gpgsign", "false")):
        subprocess.run(["git", "config", k, v], cwd=work, check=True)
    (work / ".supertool.json").write_text('{"presets": ["git"]}' + chr(10),
                                          encoding="utf-8")
    for name in ("mine.txt", "theirs.txt"):
        (work / name).write_text("1" + chr(10), encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=work, check=True)
    return work


def _run(args, cwd: Path) -> str:
    env = dict(os.environ)
    env["SUPERTOOL_COAUTHOR"] = COAUTHOR
    proc = subprocess.run(
        [sys.executable, str(SUPERTOOL), *args],
        capture_output=True, text=True, timeout=120,
        encoding="utf-8", errors="replace", cwd=str(cwd), env=env,
    )
    return proc.stdout + proc.stderr


def _git(args, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, encoding="utf-8", errors="replace").stdout


def _committed(cwd: Path):
    """Paths in HEAD's own diff — the commit, not the worktree."""
    out = _git(["show", "--name-only", "--format=", "HEAD"], cwd)
    return sorted(p for p in out.split() if p)


def _staged(cwd: Path):
    return sorted(p for p in _git(["diff", "--cached", "--name-only"],
                                  cwd).split() if p)


# ── 1. the commit is scoped to the paths it was given ────────────────────

def test_a_foreign_staged_change_does_not_ride_into_a_named_commit(
        tmp_path: Path) -> None:
    """The defect, reproduced end to end.

    `theirs.txt` is staged by somebody else. The call names `mine.txt` only.
    Before the fix the commit carried both.
    """
    work = _repo(tmp_path)
    (work / "mine.txt").write_text("mine" + chr(10), encoding="utf-8")
    (work / "theirs.txt").write_text("theirs" + chr(10), encoding="utf-8")
    subprocess.run(["git", "add", "theirs.txt"], cwd=work, check=True)

    out = _run(["git-commit:::subject:::mine.txt"], cwd=work)

    assert _committed(work) == ["mine.txt"], out
    # ...and the other change is not lost either: it is still staged, exactly
    # where its author left it.
    assert _staged(work) == ["theirs.txt"], out


def test_scoping_survives_a_path_whose_worktree_and_index_disagree(
        tmp_path: Path) -> None:
    """A file staged at one content and edited again afterwards.

    The foreign path is `theirs.txt`; only `mine.txt` is named. Neither the
    staged nor the worktree version of `theirs.txt` may reach the commit.
    """
    work = _repo(tmp_path)
    (work / "theirs.txt").write_text("staged" + chr(10), encoding="utf-8")
    subprocess.run(["git", "add", "theirs.txt"], cwd=work, check=True)
    (work / "theirs.txt").write_text("edited again" + chr(10), encoding="utf-8")
    (work / "mine.txt").write_text("mine" + chr(10), encoding="utf-8")

    out = _run(["git-commit:::subject:::mine.txt"], cwd=work)

    assert _committed(work) == ["mine.txt"], out


# ── 2. the receipt accounts for the commit, not for the worktree ─────────

def test_the_receipt_names_what_was_staged_and_left_out(tmp_path: Path) -> None:
    """The half that catches the *next* unforeseen version of this.

    Scoping the commit means a staged-only path is now silently excluded — a
    correct commit with an incomplete receipt, which is the same defect one
    layer along. The receipt has to name it.
    """
    work = _repo(tmp_path)
    (work / "mine.txt").write_text("mine" + chr(10), encoding="utf-8")
    (work / "theirs.txt").write_text("theirs" + chr(10), encoding="utf-8")
    subprocess.run(["git", "add", "theirs.txt"], cwd=work, check=True)

    out = _run(["git-commit:::subject:::mine.txt"], cwd=work)

    assert "theirs.txt" in out, out
    assert "staged" in out.lower(), out
    assert "NOT in this commit" in out, out


def test_a_clean_scoped_commit_gains_no_leftover_warning(
        tmp_path: Path) -> None:
    """The line must not become noise on the run that left nothing staged."""
    work = _repo(tmp_path)
    (work / "mine.txt").write_text("mine" + chr(10), encoding="utf-8")

    out = _run(["git-commit:::subject:::mine.txt"], cwd=work)

    assert "NOT in this commit" not in out, out


# ── 3. what must not change ──────────────────────────────────────────────

def test_the_pathless_call_still_commits_what_you_staged_by_hand(
        tmp_path: Path) -> None:
    """The deliberate whole-index route, which is the pathless one.

    This is the answer to "was committing the whole index ever wanted": yes,
    and it already has a spelling that says so. Naming paths says the
    opposite, and now behaves that way.
    """
    work = _repo(tmp_path)
    (work / "mine.txt").write_text("mine" + chr(10), encoding="utf-8")
    (work / "theirs.txt").write_text("theirs" + chr(10), encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True)

    out = _run(["git-commit:::subject"], cwd=work)

    assert _committed(work) == ["mine.txt", "theirs.txt"], out


def test_a_named_staged_deletion_is_still_committed(tmp_path: Path) -> None:
    """#324 — the path is gone from disk, and its deletion must still land."""
    work = _repo(tmp_path)
    subprocess.run(["git", "rm", "-q", "mine.txt"], cwd=work, check=True)

    out = _run(["git-commit:::drop it:::mine.txt"], cwd=work)

    assert _committed(work) == ["mine.txt"], out
    assert not (work / "mine.txt").exists()


def test_naming_a_path_during_a_conflicted_merge_still_commits(
        tmp_path: Path) -> None:
    """git refuses a partial commit during a merge, outright.

    `git commit -- <path>` implies `--only`, and git answers `fatal: cannot do
    a partial commit during a merge` with exit 128. Scoping has to stand down
    there — a merge commit is whole-index by construction — and the receipt
    has to say it did.
    """
    work = _repo(tmp_path)
    (work / "mine.txt").write_text("ours" + chr(10), encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "ours"], cwd=work, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", "side", "HEAD~1"],
                   cwd=work, check=True)
    (work / "mine.txt").write_text("theirs" + chr(10), encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "theirs"], cwd=work, check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=work, check=True)
    subprocess.run(["git", "merge", "side", "-m", "m"], cwd=work,
                   capture_output=True, text=True, encoding="utf-8",
                   errors="replace")
    (work / "mine.txt").write_text("resolved" + chr(10), encoding="utf-8")

    out = _run(["git-commit:::resolve the merge:::mine.txt"], cwd=work)

    assert "partial commit" not in out, out
    assert "HEAD after" in out and chr(10) + "Status:" not in out, out
    parents = _git(["rev-list", "--parents", "-n", "1", "HEAD"], work).split()
    assert len(parents) == 3, out


def test_named_paths_holding_nothing_are_refused_even_when_the_index_is_dirty(
        tmp_path: Path) -> None:
    """The staged pre-check has to be scoped too.

    Unscoped, it sees the foreign staged path, calls the index non-empty and
    hands git a pathspec with nothing under it — so the op's own refusal is
    replaced by git's `nothing added to commit`, exit 1, under a header that
    already printed `Staged: 1 path(s)`.
    """
    work = _repo(tmp_path)
    (work / "theirs.txt").write_text("theirs" + chr(10), encoding="utf-8")
    subprocess.run(["git", "add", "theirs.txt"], cwd=work, check=True)

    out = _run(["git-commit:::subject:::mine.txt"], cwd=work)

    assert "ERROR: nothing staged" in out, out
    assert "held no changes to stage" in out, out


def test_all_includes_what_is_already_staged(tmp_path: Path) -> None:
    """`--all` resolved to `git status`'s dirty lists, and those are not everything.

    A path that is staged and whose worktree matches the index is invisible to
    the unstaged column, so it was absent from the expansion. Before #1228
    that did not show: the commit was unscoped and swept the index in anyway.
    Scoping made the omission real — `--all` would have committed the modified
    path and silently left the staged one behind, which is the opposite of
    what the token means.
    """
    work = _repo(tmp_path)
    (work / "theirs.txt").write_text("staged" + chr(10), encoding="utf-8")
    subprocess.run(["git", "add", "theirs.txt"], cwd=work, check=True)
    (work / "mine.txt").write_text("unstaged" + chr(10), encoding="utf-8")

    out = _run(["git-commit:::subject:::" + "--all"], cwd=work)

    assert _committed(work) == ["mine.txt", "theirs.txt"], out
    assert _staged(work) == [], out


def test_all_names_every_path_it_chose_even_when_they_were_already_staged(
        tmp_path: Path) -> None:
    """`--all`'s receipt is uncapped, and that has to survive a staged tree.

    An expansion that came back empty left `paths` empty, so `all_used` was
    False and the receipt fell back to the 20-path cap — under a token whose
    contract is that the caller typed nothing, so the receipt is the only
    record of what was chosen and a capped one is a subset presented as the
    whole (#1137).
    """
    work = _repo(tmp_path)
    for n in range(25):
        (work / f"f{n:02d}.txt").write_text("x" + chr(10), encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True)

    out = _run(["git-commit:::subject:::" + "--all"], cwd=work)

    assert "more" not in out.split("Files committed:")[-1], out
    assert "f24.txt" in out, out
    assert len(_committed(work)) == 25, out


def test_all_still_refuses_a_clean_tree_with_an_empty_index(
        tmp_path: Path) -> None:
    """Widening `--all` must not turn "nothing to do" into an empty commit."""
    work = _repo(tmp_path)

    out = _run(["git-commit:::subject:::" + "--all"], cwd=work)

    assert "ERROR: nothing staged" in out, out
    assert "working tree is clean" in out, out


def test_the_change_is_findable() -> None:
    assert_change_is_findable(1228, REPO)
