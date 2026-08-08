"""#1070 — the left-behind receipt drops untracked files without saying so.

Two shapes, both live on master:

  * **modified + untracked left behind** — the header counts the untracked
    ("(1 untracked, not listed)") but the copy-paste remedy one line below
    names only the modified paths. Pasted, it commits a strict subset of what
    the receipt just accounted for, and nothing in the remedy says so.
  * **only untracked left behind** — `_left_behind_lines` returns `[]`. The
    receipt is a green tick over `Files committed: N` with a brand-new file
    still uncommitted and no mention of it anywhere. Silence is byte-for-byte
    the render of "nothing was left behind".

The second is the worse one and is the one the issue was filed from: the drop
is invisible until CI runs a test file that was never committed.

What is deliberately NOT changed: untracked files stay *counted, not listed*
(#1016, `test_untracked_files_are_counted_but_not_listed`). A worktree full of
scratch files must not print a scratch-file list. The fix is a disclosure, not
a listing — the remedy has to say it excludes them, and the untracked-only
case has to say anything at all.
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent
_COMMIT_PATH = _ROOT / "presets" / "git" / "commit.py"
_spec = importlib.util.spec_from_file_location("git_commit_1070", _COMMIT_PATH)
assert _spec is not None and _spec.loader is not None
commit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(commit)

NUL = chr(0)


def _status(stdout: str) -> Any:
    """A stand-in `git` whose `status -z` answers with *stdout*."""
    def run(args: list, timeout: Any = None) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            args=["git"] + list(args), returncode=0, stdout=stdout, stderr="",
        )
    return run


def test_untracked_only_left_behind_is_not_silent() -> None:
    """No modified tracked file, one new file: the receipt must still speak."""
    lines = commit._left_behind_lines(_status("?? tests/test_new.py" + NUL))
    assert lines, (
        "a commit that left a brand-new file uncommitted rendered nothing at "
        "all — the same bytes as a commit that left nothing behind")
    joined = "\n".join(lines)
    assert "1 untracked" in joined, joined


def test_the_untracked_only_disclosure_still_does_not_list_them() -> None:
    """#1016's decision holds: counted, never listed."""
    stdout = "".join(f"?? scratch{n}.tmp" + NUL for n in range(1, 4))
    joined = "\n".join(commit._left_behind_lines(_status(stdout)))
    assert "3 untracked" in joined, joined
    assert "scratch1.tmp" not in joined, joined


def test_the_remedy_says_it_leaves_the_untracked_out() -> None:
    """The pasteable line is a subset of the receipt; it must admit it."""
    lines = commit._left_behind_lines(
        _status(" M b.txt" + NUL + "?? tests/test_new.py" + NUL))
    joined = "\n".join(lines)
    remedy = [ln for ln in lines if "git-commit:::" in ln]
    assert remedy, joined
    assert "tests/test_new.py" not in "\n".join(remedy), joined
    assert "untracked" in joined.lower(), joined
    assert "not in the command" in joined.lower(), (
        "the remedy names three of four files and says nothing about the "
        "fourth:\n" + joined)


def test_nothing_left_behind_at_all_still_renders_nothing() -> None:
    """The common case may not gain a line (#1016)."""
    assert commit._left_behind_lines(_status("")) == []


def test_an_unanswered_status_still_declines_rather_than_counting_zero() -> None:
    def dead(args: list, timeout: Any = None) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            args=["git"] + list(args), returncode=128, stdout="",
            stderr="fatal: unable to read index",
        )

    joined = "\n".join(commit._left_behind_lines(dead))
    assert "SKIPPED" in joined, joined
    assert "0 untracked" not in joined, joined
