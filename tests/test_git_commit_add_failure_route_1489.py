"""#1489 — the `git add` failure quotes the colon route's separator rule at
every caller, including ones standing on the payload route and ones whose
pathspec holds no separator at all.

Measured twice in one afternoon by two independent agents: a payload carrying
`paths = "a.txt:::b.txt"` and one carrying `paths = ":::--all"` were both
refused with *"Paths are separated by ':::'"* — which is exactly what each had
typed. The payload route wants a TOML array; the note names the other route's
convention, so the reader re-reads the thing they got right.

Three states, and the third is the point:

  - the separator really is the likely fault  -> name it, for the route in use
  - it is the wrong route's rule              -> name the array, offer the split
  - nothing here was separated wrongly        -> say nothing beyond git's error

A fresh wrong remedy reads as a freshly checked one, which is why the third
state is silence rather than a reworded guess.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
SUPERTOOL = REPO / "supertool.py"
COAUTHOR = "Test Bot <bot@example.invalid>"

SEP_NOTE = "Paths are separated by"


def _repo(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t"),
                 ("commit.gpgsign", "false")):
        subprocess.run(["git", "config", k, v], cwd=work, check=True)
    (work / ".supertool.json").write_text(
        '{"presets": ["git"]}\n', encoding="utf-8")
    for name in ("a.txt", "b.txt"):
        (work / name).write_text("1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=work, check=True)
    for name in ("a.txt", "b.txt"):
        (work / name).write_text("2\n", encoding="utf-8")
    return work


def _run(args, cwd: Path, stdin: str = "") -> str:
    env = dict(os.environ)
    env["SUPERTOOL_COAUTHOR"] = COAUTHOR
    proc = subprocess.run(
        [sys.executable, str(SUPERTOOL), *args],
        input=stdin, capture_output=True, text=True, timeout=120,
        encoding="utf-8", errors="replace", cwd=str(cwd), env=env,
    )
    return proc.stdout + proc.stderr


def _head(work: Path) -> str:
    return subprocess.run(
        ["git", "log", "-1", "--pretty=format:%h"], cwd=work,
        capture_output=True, text=True, check=True, encoding="utf-8",
        errors="replace",
    ).stdout


# --------------------------------------------------------------------------
# The payload route: the colon form's rule is not this route's rule
# --------------------------------------------------------------------------

def test_payload_paths_joined_on_the_triple_colon_names_the_array(
        tmp_path: Path) -> None:
    work = _repo(tmp_path)
    before = _head(work)
    out = _run(["git-commit:@-"], cwd=work,
               stdin='message = "t"\npaths = "a.txt:::b.txt"\n')
    assert SEP_NOTE not in out, out
    assert "TOML array" in out, out
    assert 'paths = ["a.txt", "b.txt"]' in out, out
    assert _head(work) == before, out


def test_payload_all_token_joined_on_the_triple_colon_names_the_array(
        tmp_path: Path) -> None:
    """The second agent's spelling: `paths = ":::--all"` (#1489 comment)."""
    work = _repo(tmp_path)
    out = _run(["git-commit:@-"], cwd=work,
               stdin='message = "t"\npaths = ":::--all"\n')
    assert SEP_NOTE not in out, out
    assert 'paths = ["--all"]' in out, out


def test_payload_array_with_a_typo_gets_no_separator_note(
        tmp_path: Path) -> None:
    """A correct route, a correct shape, one path that does not exist.

    Nothing was separated wrongly, so nothing here can be said about
    separators. git already named the fault.
    """
    work = _repo(tmp_path)
    out = _run(["git-commit:@-"], cwd=work,
               stdin='message = "t"\npaths = ["a.txt", "nope.txt"]\n')
    assert "git add failed" in out, out
    assert SEP_NOTE not in out, out
    assert "TOML array" not in out, out


# --------------------------------------------------------------------------
# The colon route: the note stays where it is earned, and only there
# --------------------------------------------------------------------------

def test_colon_route_plain_typo_gets_no_separator_note(
        tmp_path: Path) -> None:
    work = _repo(tmp_path)
    out = _run(["git-commit:::msg:::nope.txt"], cwd=work)
    assert "git add failed" in out, out
    assert SEP_NOTE not in out, out


def test_colon_route_comma_guess_still_names_the_separator(
        tmp_path: Path) -> None:
    """#986's own case — unchanged, and pinned here because #1489 narrows
    the branch it lives in."""
    work = _repo(tmp_path)
    out = _run(["git-commit:::msg:::a.txt,b.txt"], cwd=work)
    assert SEP_NOTE in out, out
    assert "A ',' above is not a separator here" in out, out
    assert "git-commit:::MESSAGE:::a.txt:::b.txt" in out, out
    # It used to be printed twice — once unconditionally above the question
    # and once by `_colon_remedy`. Only the second is anyone's contract.
    assert out.count(SEP_NOTE) == 1, out


def test_colon_route_space_joined_never_reaches_git_add(
        tmp_path: Path) -> None:
    """A space makes the token prose, so `_colon_split_refusal` fires first.

    Recorded because the note this issue narrows claims ':::' is "not spaces"
    — and a space-joined pathspec cannot reach the line that says so.
    """
    work = _repo(tmp_path)
    out = _run(["git-commit:::msg:::a.txt b.txt"], cwd=work)
    assert "git add failed" not in out, out
    assert "was split on" in out, out
