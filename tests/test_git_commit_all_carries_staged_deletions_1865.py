"""#1865 — `--all` committed the added half of a `git mv` and left the deletion staged.

The receipt said so, honestly, in one line among a validator block:

    1 path(s) were already staged and are NOT in this commit

and the tree that landed held **two copies** of the moved module. Half a rename
compiles, imports, and passes any test that only touches the new path; the old
copy then sits there until something notices two definitions of the same thing.

**Mechanism, measured on git 2.46.2 rather than reasoned.** `--all` resolves to
`git status`'s dirty lists plus everything in the index, and the index read is
`git diff --cached --name-only`. Rename detection is on by default, so for a
staged `git mv a.txt b.txt` that read prints `b.txt` alone — the deletion of
`a.txt` is folded into a rename entry and its name never appears:

    $ git diff --cached --name-only
    b.txt
    $ git diff --cached --name-only --no-renames
    a.txt
    b.txt
    $ git diff --cached --diff-filter=D --name-only
    (empty)
    $ git diff --cached --diff-filter=D --name-only --no-renames
    a.txt

So `--all` never saw the old path, the commit was scoped to the new one, and the
deletion stayed in the index.

**Which of the issue's two answers this takes, and why.** `--all` carries the
deletion. It is not a redefinition: `_resolve_all_token`'s own docstring already
says the index is part of what `--all` means, and reads it for exactly that
reason. Rename detection was silently subtracting from a set the op had already
decided to include, so this restores the documented contract rather than
choosing a new one. Refusing at stage time would have been defensible — the
issue says either is — but it makes the op decline a `git mv`, the single most
common operation whose two halves must land together, and the caller's only
remedy would be to name both paths by hand.

**The control, and it is what stops the fix becoming a different op.**
"Commit everything staged" is `git-commit:::MESSAGE` with no PATHS. Widening
`--all` must not widen *that* distinction: a call that NAMES its paths must
still leave an unrelated staged path out of the commit (#1228). Without that
assertion the fix passes by committing the whole index, which is precisely the
defect #1228 exists to remove.
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
    for name in ("moved.py", "other.txt"):
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
    """Paths in HEAD's own diff, with rename detection OFF.

    `--name-only` alone would print a committed rename as its destination and
    hide the source — the same fold that caused the defect, reproduced in the
    assertion, so a half-rename would read as a whole one here.
    """
    out = _git(["show", "--name-only", "--no-renames", "--format=", "HEAD"], cwd)
    return sorted(p for p in out.split() if p)


def _staged(cwd: Path):
    out = _git(["diff", "--cached", "--name-only", "--no-renames"], cwd)
    return sorted(p for p in out.split() if p)


def test_all_carries_both_halves_of_a_staged_rename(tmp_path: Path) -> None:
    """The defect, end to end: `git mv` then `--all` must land the whole move."""
    work = _repo(tmp_path)
    subprocess.run(["git", "mv", "moved.py", "renamed.py"], cwd=work, check=True)

    out = _run(["git-commit:::subject:::" + "--all"], cwd=work)

    assert _committed(work) == ["moved.py", "renamed.py"], out
    assert _staged(work) == [], out
    assert "are already staged and are NOT" not in out, out
    assert not (work / "moved.py").exists(), "the old path must be gone from the tree"


def test_all_says_nothing_stayed_behind_after_a_rename(tmp_path: Path) -> None:
    """The receipt is the only record `--all` leaves, so it has to be right.

    A run that skimmed one warning line among a validator block is how the
    original half-rename landed. With both halves committed, the still-staged
    warning must be absent — and absent because the index is empty, not because
    the check stopped looking.
    """
    work = _repo(tmp_path)
    subprocess.run(["git", "mv", "moved.py", "renamed.py"], cwd=work, check=True)

    out = _run(["git-commit:::subject:::" + "--all"], cwd=work)

    assert "path(s) were already staged" not in out, out
    assert "renamed.py" in out, out
    assert "moved.py" in out, out


def test_naming_paths_still_leaves_an_unrelated_staged_path_alone(
        tmp_path: Path) -> None:
    """The control. Widening `--all` must not widen the scoped call (#1228).

    "Commit everything staged" is the pathless spelling, and it is a different
    op. A fix that reached its assertion by committing the whole index would
    pass the two tests above and silently undo the scoping that stops another
    process's staged work riding into someone else's commit.
    """
    work = _repo(tmp_path)
    (work / "other.txt").write_text("theirs" + chr(10), encoding="utf-8")
    subprocess.run(["git", "add", "other.txt"], cwd=work, check=True)
    (work / "moved.py").write_text("mine" + chr(10), encoding="utf-8")

    out = _run(["git-commit:::subject:::moved.py"], cwd=work)

    assert _committed(work) == ["moved.py"], out
    assert _staged(work) == ["other.txt"], out
    assert "path(s) were already staged and are NOT in this commit" in out, out


def test_a_named_staged_rename_still_commits_both_halves(tmp_path: Path) -> None:
    """The same move, named explicitly rather than through `--all`.

    Naming the destination alone is what a caller who does not know about the
    fold would type. It is a partial commit by construction and stays one — but
    naming BOTH halves must work, and must not trip the #324 abort where `git
    add` is handed a path that is gone from disk.
    """
    work = _repo(tmp_path)
    subprocess.run(["git", "mv", "moved.py", "renamed.py"], cwd=work, check=True)

    out = _run(["git-commit:::subject:::moved.py:::renamed.py"], cwd=work)

    assert "pathspec" not in out, out
    assert _committed(work) == ["moved.py", "renamed.py"], out
    assert _staged(work) == [], out


def test_all_still_refuses_a_clean_tree(tmp_path: Path) -> None:
    """Widening `--all` must not turn "nothing to do" into an empty commit."""
    work = _repo(tmp_path)

    out = _run(["git-commit:::subject:::" + "--all"], cwd=work)

    assert "ERROR: nothing staged" in out, out
    assert "working tree is clean" in out, out


def test_the_change_is_findable() -> None:
    assert_change_is_findable(1865, REPO)
