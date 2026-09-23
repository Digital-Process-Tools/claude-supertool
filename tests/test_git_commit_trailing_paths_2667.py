"""#2667 -- `message = @rest` swallows a trailing `paths = [...]` line.

`_load_at_file_raw`'s generic `@rest` split (#1868) takes everything after
a `FIELD = @rest` marker verbatim as FIELD's value, with no scanning of
that tail for a later sibling field. A `paths = ["a.py"]` line written
after the message text is therefore never read as its own field -- it
becomes the last line of the commit body, and `git-commit` reaches
commit.py with no PATHS at all. With foreign changes already staged, the
documented "omitting PATHS commits the index exactly as it stands"
behaviour applies: the foreign file is committed under this message, the
file the caller named is left out, and the receipt says PASS.

This file pins git-commit's own defence: when NO paths were given on the
argument list, a trailing line shaped like `paths = [...]` is refused
before anything is staged.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys

from pathlib import Path

REPO = Path(__file__).parent.parent
SUPERTOOL = REPO / "supertool.py"

_COMMIT_PATH = REPO / "presets" / "git" / "commit.py"
_spec = importlib.util.spec_from_file_location("git_commit_2667", _COMMIT_PATH)
assert _spec is not None and _spec.loader is not None
commit_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(commit_mod)


def _repo(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t"),
                 ("commit.gpgsign", "false")):
        subprocess.run(["git", "config", k, v], cwd=work, check=True)
    (work / ".supertool.json").write_text('{"presets": ["git"]}\n',
                                          encoding="utf-8")
    (work / "a.txt").write_text("1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=work, check=True)
    return work


def _run(args: list, cwd: Path, input_bytes: bytes = None) -> str:
    # No `env=` at all -- the child inherits this process's environment,
    # which `tests/_pathenv_scan.py` (#1151) reads as the always-safe case.
    # An explicit `env=None` is a DIFFERENT AST shape to that scanner (a
    # literal it cannot classify, not "no env kwarg"), and was flagged
    # `unresolved` on CI for exactly that reason.
    proc = subprocess.run(
        [sys.executable, str(SUPERTOOL), *args],
        capture_output=True, timeout=120, cwd=str(cwd),
        input=input_bytes,
    )
    out = proc.stdout + proc.stderr
    return out.decode("utf-8", errors="replace")


def _head_subject(work: Path) -> str:
    return subprocess.run(
        ["git", "log", "-1", "--pretty=format:%s"], cwd=work,
        capture_output=True, text=True, check=True, encoding="utf-8",
        errors="replace",
    ).stdout


# --- unit level: the hazard predicate itself -------------------------------


def test_trailing_paths_hazard_fires_when_paths_is_empty() -> None:
    msg = 'fix: subject\n\nbody\npaths = ["a.py"]'
    assert commit_mod._trailing_paths_hazard(msg, []) is True


def test_trailing_paths_hazard_is_none_when_paths_were_given() -> None:
    """Positive control: the same trailing line is not a hazard once the
    caller also named real paths through the ordinary argument list --
    that is the case where the sentence is plausibly commit-body prose."""
    msg = 'fix: subject\n\nbody\npaths = ["a.py"]'
    assert commit_mod._trailing_paths_hazard(msg, ["a.py"]) is False


def test_trailing_paths_hazard_is_none_for_an_ordinary_message() -> None:
    msg = "fix: rename the paths helper\n\nbody line one\nbody line two"
    assert commit_mod._trailing_paths_hazard(msg, []) is False


def test_trailing_paths_hazard_only_looks_at_the_last_line() -> None:
    """A mid-body mention of `paths = [` that is NOT the last line is
    ordinary prose, not a swallowed field."""
    msg = 'fix: rename\n\npaths = ["a"] used to be the old name\nfinal line'
    assert commit_mod._trailing_paths_hazard(msg, []) is False


def test_trailing_paths_hazard_tolerates_trailing_blank_lines() -> None:
    msg = 'fix: subject\n\nbody\npaths = ["a.py"]\n\n\n'
    assert commit_mod._trailing_paths_hazard(msg, []) is True


# --- end-to-end: the issue's own reproduction shape -------------------------


def test_message_rest_tail_swallowing_paths_is_refused_before_staging(
        tmp_path: Path) -> None:
    """The issue's own reproduction: a `message = @rest` payload whose tail
    ends in a `paths = [...]` line, with a foreign file already staged and
    the caller's own file untracked. Before the fix this committed the
    foreign file under a mangled message and reported PASS."""
    work = _repo(tmp_path)
    (work / "foreign.txt").write_text("foreign\n", encoding="utf-8")
    (work / "a.py").write_text("mine\n", encoding="utf-8")
    subprocess.run(["git", "add", "foreign.txt"], cwd=work, check=True)

    payload = (
        "message = @rest\n"
        "fix: subject\n\n"
        "body\n"
        'paths = ["a.py"]\n'
    )
    out = _run(["git-commit:@-"], cwd=work, input_bytes=payload.encode("utf-8"))

    assert "ERROR" in out, out
    assert "2667" in out, out
    assert _head_subject(work) == "seed"
    # Nothing committed -- the foreign file is still staged, not swept in.
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=work,
        capture_output=True, text=True, check=True,
        encoding="utf-8", errors="replace",
    ).stdout
    assert "foreign.txt" in status


def test_ordinary_message_with_paths_given_is_unaffected(
        tmp_path: Path) -> None:
    """Positive control for the end-to-end path: paths given normally, no
    trailing-paths shape in the message -- commits exactly as always."""
    work = _repo(tmp_path)
    (work / "a.py").write_text("mine\n", encoding="utf-8")

    out = _run(["git-commit:::fix: subject:::a.py"], cwd=work)

    assert "ERROR" not in out, out
    assert _head_subject(work) == "fix: subject"


def test_trailing_paths_shape_with_real_paths_given_is_not_refused(
        tmp_path: Path) -> None:
    """A message whose last line happens to be shaped like `paths = [...]`
    is not refused when the caller ALSO named real paths -- that combination
    means the shape is body content, not a swallowed field."""
    work = _repo(tmp_path)
    (work / "a.py").write_text("mine\n", encoding="utf-8")

    out = _run(
        ["git-commit:::fix: subject\n\nbody\npaths = [\"x\"]:::a.py"],
        cwd=work)

    assert "ERROR" not in out, out
