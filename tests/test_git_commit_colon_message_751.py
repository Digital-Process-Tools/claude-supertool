"""End-to-end pins for git-commit's ':'-in-message refusal (issue #751).

`git-commit:MESSAGE` is tokenized on ':' by supertool's colon CLI, so a
Conventional Commits subject — `fix(rector): link the importer` — is split and
everything after the first colon is handed to `git add` as a pathspec. The
reported symptom is a `git add failed: fatal: pathspec ' link the importer'`
error that names neither the message nor the tokenizer.

These tests go through supertool.py itself rather than commit.py's `main()`,
because the split happens in the tokenizer: a test that hands commit.py a
pre-split argv can only assert what commit.py does with the wreckage, not that
the wreckage is recognised as such.

The fix is a REFUSAL, not a re-parse. Folding trailing segments back into the
message would require guessing whether a segment is prose or a pathspec, and a
wrong guess in the fold-back direction commits whatever happens to be staged
under a mangled message — a success-shaped receipt for the wrong fileset. So
the tests below pin two things in equal measure: the ambiguous case is refused
with nothing staged and nothing committed, and every unambiguous case —
`:::MESSAGE:::PATHS`, the `@payload` route, a colon-free message with paths, a
staged deletion, an ordinary typo'd pathspec — behaves exactly as before.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
SUPERTOOL = REPO / "supertool.py"

COAUTHOR = "Test Bot <bot@example.invalid>"

CONVENTIONAL = "fix(rector): link the CSV importer to its new test"


def _repo(tmp_path: Path) -> Path:
    """Throwaway git repo wired to the shipped git preset, one commit deep."""
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=work, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=work, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=work, check=True)
    (work / ".supertool.json").write_text('{"presets": ["git"]}\n')
    (work / "a.txt").write_text("hi\n")
    subprocess.run(["git", "add", "a.txt"], cwd=work, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=work, check=True)
    return work


def _run(args: list[str], cwd: Path, stdin: str = "") -> tuple[int, str, str]:
    env = dict(os.environ)
    env["SUPERTOOL_COAUTHOR"] = COAUTHOR
    proc = subprocess.run(
        [sys.executable, str(SUPERTOOL), *args],
        input=stdin, capture_output=True, text=True, timeout=60,
        encoding="utf-8", errors="replace", cwd=str(cwd), env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _head(cwd: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True,
        text=True, check=True,
    ).stdout.strip()


def _subject(cwd: Path) -> str:
    return subprocess.run(
        ["git", "log", "-1", "--pretty=format:%s"], cwd=cwd,
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout


def _staged(cwd: Path) -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only"], cwd=cwd,
        capture_output=True, text=True, check=True,
    ).stdout
    return [line for line in out.splitlines() if line.strip()]


# --------------------------------------------------------------------------
# The bug: the reported reproduction.
# --------------------------------------------------------------------------

def test_conventional_message_is_refused_not_handed_to_git_add(tmp_path: Path) -> None:
    """The exact reproduction from #751 — refused, and named as a split."""
    work = _repo(tmp_path)
    (work / "b.txt").write_text("b\n")
    subprocess.run(["git", "add", "b.txt"], cwd=work, check=True)
    before = _head(work)

    code, out, err = _run([f"git-commit:{CONVENTIONAL}"], cwd=work)

    assert code != 0, f"expected a refusal, got success: {out}"
    # The old failure blamed git add / pathspecs, which is nowhere near cause.
    assert "pathspec" not in out, out
    assert "git add failed" not in out, out
    # The new failure names the actual cause and both working routes.
    assert "split" in out.lower(), out
    assert ":::" in out, out
    assert "@-" in out, out
    # And the reconstructed message is offered back verbatim.
    assert CONVENTIONAL in out, out
    assert _head(work) == before, "HEAD moved on a refused commit"


def test_refusal_commits_nothing_and_stages_nothing(tmp_path: Path) -> None:
    """A refused call must not move HEAD or change the index (#751)."""
    work = _repo(tmp_path)
    (work / "b.txt").write_text("b\n")
    subprocess.run(["git", "add", "b.txt"], cwd=work, check=True)
    before = _head(work)
    staged_before = _staged(work)

    code, out, _ = _run([f"git-commit:{CONVENTIONAL}"], cwd=work)

    assert code != 0, out
    assert "split" in out.lower(), out
    assert _head(work) == before, "HEAD moved on a refused commit"
    assert _staged(work) == staged_before, "index changed on a refused commit"


# --------------------------------------------------------------------------
# Everything that must keep working, unchanged.
# --------------------------------------------------------------------------

def test_triple_colon_message_with_paths_still_commits(tmp_path: Path) -> None:
    """The documented form `git-commit:::MESSAGE:::PATHS`."""
    work = _repo(tmp_path)
    (work / "b.txt").write_text("b\n")

    code, out, err = _run([f"git-commit:::{CONVENTIONAL}:::b.txt"], cwd=work)

    assert code == 0, f"stdout={out} stderr={err}"
    assert _subject(work) == CONVENTIONAL
    assert _staged(work) == []


def test_payload_stdin_route_still_commits(tmp_path: Path) -> None:
    """The `@payload` route — message with a colon plus an explicit path."""
    work = _repo(tmp_path)
    (work / "b.txt").write_text("b\n")
    payload = f"message = '''{CONVENTIONAL}'''\npaths = [\"b.txt\"]\n"

    code, out, err = _run(["git-commit:@-"], cwd=work, stdin=payload)

    assert code == 0, f"stdout={out} stderr={err}"
    assert _subject(work) == CONVENTIONAL


def test_colon_free_message_with_paths_still_commits(tmp_path: Path) -> None:
    """Single-colon route, colon-free message: unambiguous, must still work."""
    work = _repo(tmp_path)
    (work / "b.txt").write_text("b\n")

    code, out, err = _run(["git-commit:add the b file:b.txt"], cwd=work)

    assert code == 0, f"stdout={out} stderr={err}"
    assert _subject(work) == "add the b file"


def test_staged_deletion_path_is_not_mistaken_for_prose(tmp_path: Path) -> None:
    """A `git rm`'d path is gone from disk but is still a real pathspec (#324).

    This is the case that makes 'resolves to an existing file' unusable as a
    discriminator: folding it into the message would commit the deletion under
    a mangled subject.
    """
    work = _repo(tmp_path)
    subprocess.run(["git", "rm", "-q", "a.txt"], cwd=work, check=True)

    code, out, err = _run(["git-commit:::chore: drop a:::a.txt"], cwd=work)

    assert code == 0, f"stdout={out} stderr={err}"
    assert _subject(work) == "chore: drop a"


def test_unknown_pathlike_still_reports_the_pathspec_error(tmp_path: Path) -> None:
    """A path-shaped token that git does not know is still git's error to give.

    `nope.lock` may be a typo or a path the caller expected to exist; supertool
    has no basis to decide it is really message text, so it must not.
    """
    work = _repo(tmp_path)

    code, out, err = _run(["git-commit:::chore bump:::nope.lock"], cwd=work)

    assert code != 0, out
    assert "pathspec" in out, out
