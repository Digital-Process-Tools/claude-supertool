"""#1015: a conflicted `_supertool.py` took the whole tool down with a traceback.

The issue is filed as "`git-conflicts` dies parsing its own conflict markers".
It does not: `conflicts.py` reads the file as text and never parses Python. The
failure is one layer lower and much wider — `supertool.py` line 64 is
`import _supertool`, and a `_supertool.py` carrying live `<<<<<<<` markers is
not valid Python, so **every** op is unavailable for as long as the conflict
exists, with a `SyntaxError` traceback as the only explanation:

    File ".../_supertool.py", line 17095
        >>>>>>> 4c5cfa8 (fix(payload): ...)
                ^
    SyntaxError: invalid decimal literal

A traceback pointing at a `>>>>>>>` reads as *the tool is broken*, which sends
the operator to raw `git diff --diff-filter=U` plus `awk` — the hand-rolled
resolver this repo has already been bitten by — or to the global `supertool`,
which inside a branch worktree is the mixed-tree invocation #1012 forbids.

Three states, not two (docs/validators.md, "Declining instead of guessing").
The shim cannot run any op, and it must not pretend otherwise; what it can do
is name *why*, and name a recovery that does not go through the module under
conflict. `presets/git/conflicts.py` and `presets/git/resolve.py` import only
`_git_common` and `_env`, so they run standalone against this tree — which the
last test here proves rather than assumes.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

_CONFLICTED_TAIL = (
    "\n"
    "<<<<<<< HEAD\n"
    "_MARKER = 1\n"
    "=======\n"
    "_MARKER = 2\n"
    ">>>>>>> 4c5cfa8 (fix(payload): a doubled backslash)\n"
)


def _install(dest: Path) -> None:
    for src in sorted(REPO_ROOT.glob("*.py")):
        shutil.copy2(src, dest / src.name)


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "supertool.py", *args],
        cwd=str(cwd), capture_output=True, encoding="utf-8", errors="replace",
    )


def _conflicted_install(tmp_path: Path) -> Path:
    _install(tmp_path)
    impl = tmp_path / "_supertool.py"
    impl.write_text(impl.read_text(encoding="utf-8") + _CONFLICTED_TAIL,
                    encoding="utf-8")
    return tmp_path


def test_a_conflicted_core_is_named_not_tracebacked(tmp_path: Path) -> None:
    root = _conflicted_install(tmp_path)
    proc = _run(root, "git-conflicts")

    assert proc.returncode != 0, "a tool that cannot run any op exited 0"
    assert "Traceback" not in proc.stderr, (
        "the operator gets a Python traceback pointing at a conflict marker, "
        "which reads as 'the tool is broken' rather than 'the tool cannot "
        f"describe this state':\n{proc.stderr}"
    )
    assert "conflict marker" in proc.stderr.lower(), (
        f"the refusal does not name what is actually wrong:\n{proc.stderr}"
    )
    assert "_supertool.py" in proc.stderr, proc.stderr


def test_the_refusal_names_the_marker_lines(tmp_path: Path) -> None:
    """Which lines, so the reader can go straight to them."""
    root = _conflicted_install(tmp_path)
    expected = len((root / "_supertool.py").read_text(encoding="utf-8").splitlines())
    proc = _run(root, "git-conflicts")

    # The `>>>>>>>` closer is the last line of the file we just wrote.
    assert str(expected) in proc.stderr, (
        f"line {expected} (the `>>>>>>>` closer) is not named:\n{proc.stderr}"
    )


def test_the_refusal_says_every_op_is_down_not_just_this_one(tmp_path: Path) -> None:
    """The title reads narrower than the defect; the message must not.

    An operator told `git-conflicts` is unavailable will try `read` next.
    """
    root = _conflicted_install(tmp_path)
    for op in ("git-conflicts", "read:supertool.py", "version"):
        proc = _run(root, op)
        assert proc.returncode != 0, op
        assert "Traceback" not in proc.stderr, f"{op}:\n{proc.stderr}"
    joined = _run(root, "version").stderr.lower()
    assert "no op can run" in joined, (
        f"nothing says the whole tool is down, only this invocation:\n{joined}"
    )


def test_the_refusal_does_not_claim_the_tree_is_clean(tmp_path: Path) -> None:
    """An absence produced by the tool must not read as an absence in the world."""
    root = _conflicted_install(tmp_path)
    err = _run(root, "git-conflicts").stderr
    lowered = err.lower()
    assert "no conflict" not in lowered
    assert "not saying" in lowered or "not a report" in lowered, (
        "nothing disclaims that this is a statement about the repository:\n"
        + err
    )


def test_the_prescribed_recovery_avoids_the_module_under_conflict(
    tmp_path: Path,
) -> None:
    """The remedy must not be the global binary (#1012's mixed tree)."""
    root = _conflicted_install(tmp_path)
    err = _run(root, "git-conflicts").stderr

    assert "presets/git/conflicts.py" in err.replace("\\", "/"), (
        f"no recovery that bypasses the broken core is offered:\n{err}"
    )
    assert "presets/git/resolve.py" in err.replace("\\", "/"), err
    # A bare `./supertool` / `supertool 'op'` here is the mixed-tree
    # invocation: it resolves to another checkout's core.
    assert "./supertool '" not in err, (
        "the remedy prescribes the very invocation the worktree rule forbids:\n"
        + err
    )


def test_an_ordinary_syntax_error_is_not_reported_as_a_conflict(
    tmp_path: Path,
) -> None:
    """Only markers get the conflict story; anything else keeps its own."""
    _install(tmp_path)
    impl = tmp_path / "_supertool.py"
    impl.write_text(impl.read_text(encoding="utf-8") + "\ndef (:\n",
                    encoding="utf-8")

    proc = _run(tmp_path, "version")
    assert proc.returncode != 0
    assert "contains git conflict markers" not in proc.stderr, (
        "a plain syntax error was diagnosed as a merge conflict:\n"
        + proc.stderr
    )
    assert "no conflict markers" in proc.stderr, proc.stderr
    assert "SyntaxError" in proc.stderr or "syntax" in proc.stderr.lower(), \
        proc.stderr


def test_the_recovery_presets_really_run_without_the_core() -> None:
    """The prescription is only a remedy if it works. Run it, in this tree.

    `presets/git/conflicts.py` must import and render with `_supertool`
    unimportable — i.e. it must not reach back through the shim.
    """
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "presets" / "git" / "conflicts.py")],
        cwd=str(REPO_ROOT), capture_output=True, encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "# git-conflicts" in proc.stdout, proc.stdout
