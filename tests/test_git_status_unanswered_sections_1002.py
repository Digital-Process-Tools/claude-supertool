"""#1002 — a `git-status` section that could not be computed must say so.

#1002 was filed as "the working-tree section vanished in a worktree with six
dirty files". That render does **not** reproduce: with a `git status` that
answers, the section prints (see `test_a_dirty_tree_still_lists_its_files`,
which is the control and passes on the unfixed code).

What is real, and is what the issue's own "fix shape" paragraph asks for, is
the other half: when `git status --porcelain=v1` does **not** answer — a held
index lock, a corrupt index, a timeout — the whole working-tree section is
simply not printed. The reader looking at the place where it belongs sees
nothing, and nothing is exactly the render of a clean tree. The only
disclosure is a global footer at the very bottom that says "sections that
depend on them are missing" without naming which.

Three states, not two (docs/validators.md, "Declining instead of guessing"):

    ## Working tree: clean                  — it looked, there was nothing
    ## Working tree (N changes)             — it looked, here they are
    ## Working tree: UNKNOWN — …            — it could not look

The same hole, same file, same shape, for `git stash list`.

These tests would all pass on code that does nothing only if silence were an
acceptable third render, which is the defect.
"""
from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

_ROOT = Path(__file__).parent.parent
_STATUS_PATH = _ROOT / "presets" / "git" / "status.py"
_spec = importlib.util.spec_from_file_location("git_status_1002", _STATUS_PATH)
assert _spec is not None and _spec.loader is not None
status = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(status)


def _ok(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["git"], returncode=0, stdout=stdout, stderr="")


def _dead(returncode: int, stderr: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["git"], returncode=returncode, stdout="", stderr=stderr
    )


# A working tree with two modified tracked files and one untracked file.
_DIRTY = " M keep.txt\n M seed.txt\n?? n1.txt\n"


def _render(monkeypatch, *, status_result, stash_result) -> str:
    """Run `git-status` against a scripted git, return everything it printed.

    Every call other than the two under test answers plausibly, so the render
    is the ordinary one and the only thing that varies is the subject.
    """
    def fake(args, timeout=None):
        head = args[0] if args else ""
        if head == "status":
            return status_result
        if head == "stash":
            return stash_result
        if head == "branch":
            return _ok("* fix/1002 abc1234 [origin/fix/1002] subject\n")
        if head == "rev-parse":
            if "--abbrev-ref" in args:
                return _ok("fix/1002\n")
            return _dead(1, "")
        if head == "rev-list":
            return _ok("0\t0\n")
        if head == "log":
            return _ok("abc1234 2026-08-08 t | subject\n")
        if head == "for-each-ref":
            return _ok("")
        return _dead(1, "")

    monkeypatch.setattr(status, "_spawn_git", fake)
    monkeypatch.setattr(
        status, "_hosted_request", lambda cmd: None
    )
    monkeypatch.setattr(sys, "argv", ["status.py"])
    buf = io.StringIO()
    with redirect_stdout(buf):
        status.main()
    return buf.getvalue()


# --- The control: the render the issue reported as missing is not missing ---


def test_a_dirty_tree_still_lists_its_files(monkeypatch) -> None:
    """#1002 as filed does not reproduce — pinned so nobody re-files it."""
    out = _render(monkeypatch, status_result=_ok(_DIRTY), stash_result=_ok(""))
    assert "## Working tree (3 changes)" in out
    assert "keep.txt" in out and "seed.txt" in out and "n1.txt" in out
    assert "UNKNOWN" not in out


def test_a_clean_tree_says_clean(monkeypatch) -> None:
    out = _render(monkeypatch, status_result=_ok(""), stash_result=_ok(""))
    assert "## Working tree: clean" in out
    assert "UNKNOWN" not in out


# --- The defect: a section that could not be computed rendered as silence ---


def test_an_unanswered_status_call_renders_a_section_not_silence(monkeypatch) -> None:
    """The reader must find something where the working tree belongs."""
    out = _render(
        monkeypatch,
        status_result=_dead(128, "fatal: unable to read index"),
        stash_result=_ok(""),
    )
    assert "## Working tree" in out, (
        "the section was omitted entirely; a reader scrolling to where it "
        "belongs finds nothing, which is the render of a clean tree"
    )
    line = next(l for l in out.splitlines() if l.startswith("## Working tree"))
    assert "UNKNOWN" in line
    # It may *deny* being clean; it may not claim it.
    assert "## Working tree: clean" not in out
    # The CLI's own words, so the reader knows to unlock the index rather
    # than to raise a timeout that was never the problem.
    assert "unable to read index" in out


def test_a_timed_out_status_call_says_it_timed_out(monkeypatch) -> None:
    out = _render(
        monkeypatch,
        status_result=_dead(status.TIMEOUT_RC, "timed out after 5s"),
        stash_result=_ok(""),
    )
    line = next(l for l in out.splitlines() if l.startswith("## Working tree"))
    assert "UNKNOWN" in line
    assert "timed out after 5s" in out


def test_an_unanswered_stash_call_renders_a_section_not_silence(monkeypatch) -> None:
    """Same hole, same file: no stashes and no answer are not the same."""
    out = _render(
        monkeypatch,
        status_result=_ok(_DIRTY),
        stash_result=_dead(128, "fatal: bad object refs/stash"),
    )
    assert "## Stashes" in out
    line = next(l for l in out.splitlines() if l.startswith("## Stashes"))
    assert "UNKNOWN" in line
    assert "bad object" in out


def test_an_answered_empty_stash_list_stays_silent(monkeypatch) -> None:
    """The common case gains no line — the marker must not become noise."""
    out = _render(monkeypatch, status_result=_ok(_DIRTY), stash_result=_ok(""))
    assert "## Stashes" not in out


def test_the_summary_footer_survives_alongside_the_section_marker(monkeypatch) -> None:
    """Both renderings, deliberately: the marker is where the reader looks,
    the footer is what survives `| tail` and what a grep matches."""
    out = _render(
        monkeypatch,
        status_result=_dead(128, "fatal: unable to read index"),
        stash_result=_ok(""),
    )
    assert status.INCOMPLETE_MARKER in out
    assert "## Working tree" in out
