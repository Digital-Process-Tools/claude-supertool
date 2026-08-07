"""#1003 — git-commit's two refusals must name the remedy, not just the fault.

#1003's first claim — "git-commit has no @payload route" — is **false**, and
was already false when #400 claimed it: the route landed in #340 and
tests/test_git_commit_payload_route.py pins the bytes it commits. Two pins
below keep that on the record so it is not re-filed a third time.

What is real is everything the report said *around* that claim:

1. A payload that will not parse gets a raw TOML error naming a line and a
   column. It says the call is wrong. It does not say what the right call
   looks like, and both reporters guessed the key names.
2. `git-commit:::@file:::path` is refused with "takes the @reference as the
   only argument" — same shape, same gap.
3. `git-commit` refuses an unstaged tree with "nothing staged", while holding
   the list of what is unstaged and not printing it. The remedy the caller
   then reaches for is a raw `git add -A`, i.e. the command the op exists to
   replace.

The refusals themselves all stay — none of these tests asks the op to commit
anything it was not told to commit.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
SUPERTOOL = REPO / "supertool.py"
COAUTHOR = "Test Bot <bot@example.invalid>"


def _repo(tmp_path: Path) -> Path:
    """Throwaway repo wired to the shipped git preset, with one commit."""
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t"),
                 ("commit.gpgsign", "false")):
        subprocess.run(["git", "config", k, v], cwd=work, check=True)
    (work / ".supertool.json").write_text('{"presets": ["git"]}\n', encoding="utf-8")
    (work / "a.txt").write_text("1\n", encoding="utf-8")
    (work / "b.txt").write_text("1\n", encoding="utf-8")
    sub = work / "sub"
    sub.mkdir()
    (sub / "c.txt").write_text("1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=work, check=True)
    return work


def _run(args: list[str], cwd: Path, stdin: str = "") -> str:
    env = dict(os.environ)
    env["SUPERTOOL_COAUTHOR"] = COAUTHOR
    proc = subprocess.run(
        [sys.executable, str(SUPERTOOL), *args],
        input=stdin, capture_output=True, text=True, timeout=120,
        encoding="utf-8", errors="replace", cwd=str(cwd), env=env,
    )
    return proc.stdout + proc.stderr


def _head_subject(work: Path) -> str:
    return subprocess.run(
        ["git", "log", "-1", "--pretty=format:%s"], cwd=work,
        capture_output=True, text=True, check=True, encoding="utf-8",
    ).stdout


# --- 1. A payload that will not parse must name the keys it wanted ---------


def test_unparseable_payload_names_the_payload_keys(tmp_path: Path) -> None:
    """The 'obvious thing to write' — a plain message file — must land on an
    error that names `message` and `paths`, not only a line and a column."""
    work = _repo(tmp_path)
    (work / "msg.txt").write_text(
        "feat(git): a subject\n\nbody line: here\n", encoding="utf-8",
    )
    out = _run(["git-commit:@msg.txt"], cwd=work)
    assert "ERROR" in out
    assert "message" in out, out
    assert "paths" in out, out
    assert "git-commit:@-" in out, out
    # And it is still a refusal: nothing was committed under the file's bytes.
    assert _head_subject(work) == "seed"


def test_payload_missing_the_message_field_names_the_keys(tmp_path: Path) -> None:
    """A near-miss key (`mesage`) parses as TOML and must get the same help."""
    work = _repo(tmp_path)
    (work / "msg.toml").write_text('mesage = "typo"\n', encoding="utf-8")
    out = _run(["git-commit:@msg.toml"], cwd=work)
    assert "ERROR" in out
    assert "message" in out and "paths" in out, out
    assert _head_subject(work) == "seed"


def test_at_reference_in_the_colon_slot_names_the_payload_keys(tmp_path: Path) -> None:
    """`git-commit:::@file:::path` — the exact call one reporter made."""
    work = _repo(tmp_path)
    (work / "msg.txt").write_text("subject\n", encoding="utf-8")
    out = _run(["git-commit:::@msg.txt:::a.txt"], cwd=work)
    assert "ERROR" in out
    assert "message" in out and "paths" in out, out
    assert _head_subject(work) == "seed"


# --- 2. The route the report said did not exist ---------------------------


def test_the_payload_route_exists_and_commits(tmp_path: Path) -> None:
    """#1003's first claim, refuted in place so it is not re-filed."""
    work = _repo(tmp_path)
    (work / "a.txt").write_text("2\n", encoding="utf-8")
    out = _run(
        ["git-commit:@-"], cwd=work,
        stdin='message = """feat: subject: with colons"""\npaths = ["a.txt"]\n',
    )
    assert "ERROR" not in out, out
    assert _head_subject(work) == "feat: subject: with colons"


# --- 3. "nothing staged" must name what is unstaged ------------------------


def test_nothing_staged_refusal_names_the_unstaged_files(tmp_path: Path) -> None:
    """The op holds the list. Withholding it sends the caller to `git add -A`."""
    work = _repo(tmp_path)
    (work / "a.txt").write_text("2\n", encoding="utf-8")
    (work / "sub" / "c.txt").write_text("2\n", encoding="utf-8")
    (work / "new.txt").write_text("n\n", encoding="utf-8")

    out = _run(["git-commit:::a message"], cwd=work)

    assert "nothing staged" in out
    assert "a.txt" in out, out
    # git reports paths with '/' on every platform, Windows included.
    assert "sub/c.txt" in out, out
    assert "new.txt" in out, out
    # The remedy must be an op, not the raw command the repo's own hook flags.
    assert "git-commit:::" in out, out
    assert _head_subject(work) == "seed"


def test_nothing_staged_refusal_separates_tracked_from_untracked(tmp_path: Path) -> None:
    """A modified tracked file and a brand-new file are different decisions."""
    work = _repo(tmp_path)
    (work / "a.txt").write_text("2\n", encoding="utf-8")
    (work / "new.txt").write_text("n\n", encoding="utf-8")
    out = _run(["git-commit:::a message"], cwd=work)
    lowered = out.lower()
    assert "modified" in lowered and "untracked" in lowered, out


def test_nothing_staged_on_a_genuinely_clean_tree_says_so(tmp_path: Path) -> None:
    """Three states: nothing staged *and* nothing to stage is not the same
    situation as nothing staged with five files waiting."""
    work = _repo(tmp_path)
    out = _run(["git-commit:::a message"], cwd=work)
    assert "nothing staged" in out
    assert "clean" in out.lower(), out
    assert _head_subject(work) == "seed"
