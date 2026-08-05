"""Branch echo on mutating ops (#381) and edit-miss diagnostics (#380).

Two near-misses in one session were the same shape — right file, wrong branch —
and one of them surfaced as `ERROR: old string not found`, which is a confusing
symptom for "you are on the wrong branch": the first hypothesis is a bad anchor,
and the next move is a `read` round-trip to check it.

supertool is the thing that knows the branch, and the thing holding the file
content when the anchor misses. Both are nearly free to report.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import supertool


@pytest.fixture(autouse=True)
def _clear_branch_cache():
    supertool._BRANCH_CACHE[0] = None
    yield
    supertool._BRANCH_CACHE[0] = None


def _git(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(tmp_path), *args],
        capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace",
    )


def _repo(tmp_path: Path) -> Path:
    """A throwaway repo. Every call is scoped with `git -C tmp_path`, so the
    suite's own git state is never touched (conftest._guard_repo_git_state)."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    return tmp_path


# ---------------------------------------------------------------------------
# _current_branch
# ---------------------------------------------------------------------------

def test_branch_reads_the_checked_out_branch(tmp_path: Path, monkeypatch) -> None:
    _repo(tmp_path)
    (tmp_path / "f.txt").write_text("x\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    _git(tmp_path, "checkout", "-q", "-b", "my-feature")
    monkeypatch.chdir(tmp_path)

    # #650: this failed once under `-n auto` from a pre-push hook, reporting
    # `''` where a branch was expected, and blocked a push it had nothing to
    # say about. `''` was then the value for both "no branch here" and "git
    # never answered", so the test could not tell a product defect from a
    # stalled subprocess and asserted the first.
    #
    # It declines instead — no retry, no second attempt, and no green (a skip
    # is reported, with the reason git gave). This can only trigger when git
    # itself did not answer: `why` is empty on every healthy read, pinned by
    # test_git_timeout_disclosure_650.py::test_a_healthy_read_reports_no_reason,
    # so a product that returns the wrong branch still fails here.
    branch, why = supertool._branch_reading()
    if why:
        pytest.skip(f"git did not answer on this runner, so what this test is "
                    f"about is unobservable — {why}")
    assert branch == "my-feature"
    assert supertool._current_branch() == "my-feature"


def test_branch_resolves_an_unborn_branch(tmp_path: Path, monkeypatch) -> None:
    """`git init` with no commit yet: rev-parse fails here, symbolic-ref works."""
    _repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert supertool._current_branch() == "main"


def test_branch_reports_detached_head(tmp_path: Path, monkeypatch) -> None:
    _repo(tmp_path)
    (tmp_path / "f.txt").write_text("x\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    _git(tmp_path, "checkout", "-q", "--detach")
    monkeypatch.chdir(tmp_path)
    assert supertool._current_branch().startswith("detached HEAD at ")


def test_branch_is_empty_outside_a_repo(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert supertool._current_branch() == ""


def test_branch_is_cached_for_the_process(tmp_path: Path, monkeypatch) -> None:
    """A batch of edits must not pay a subprocess each."""
    calls = []
    real_run = subprocess.run

    def counting_run(args, *a, **kw):
        if args and args[0] == "git":
            calls.append(args)
        return real_run(args, *a, **kw)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(supertool.subprocess, "run", counting_run)
    supertool._current_branch()
    first = len(calls)
    supertool._current_branch()
    supertool._current_branch()
    assert len(calls) == first


# ---------------------------------------------------------------------------
# branch footer on mutating ops
# ---------------------------------------------------------------------------

def test_footer_on_successful_edit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(supertool, "_branch_reading", lambda: ("my-feature", ""))
    f = tmp_path / "x.py"
    f.write_text("a = 1\n")
    out = supertool.dispatch(f"edit:::a = 1:::a = 2:::{f}")
    assert out.rstrip().endswith("[branch: my-feature]")


def test_footer_on_failed_edit(tmp_path: Path, monkeypatch) -> None:
    """The moment a wrong-branch hypothesis is worth having."""
    monkeypatch.setattr(supertool, "_branch_reading", lambda: ("my-feature", ""))
    f = tmp_path / "x.py"
    f.write_text("a = 1\n")
    out = supertool.dispatch(f"edit:::nope = 1:::a = 2:::{f}")
    assert "ERROR: old string not found" in out
    assert "[branch: my-feature]" in out


@pytest.mark.parametrize("op_arg", [
    "paste:::{f}:::new content",
    "replace:::a = 1:::a = 2:::{f}",
    "replace_lines:::{f}:::1:::1:::a = 2",
    "vim:::{f}:::/a = 1\\edaw",
])
def test_footer_on_every_mutating_op(tmp_path: Path, monkeypatch, op_arg: str) -> None:
    monkeypatch.setattr(supertool, "_branch_reading", lambda: ("my-feature", ""))
    f = tmp_path / "x.py"
    f.write_text("a = 1\n")
    out = supertool.dispatch(op_arg.format(f=f))
    assert "[branch: my-feature]" in out


def test_no_footer_on_read_ops(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(supertool, "_branch_reading", lambda: ("my-feature", ""))
    f = tmp_path / "x.py"
    f.write_text("a = 1\n")
    for arg in (f"read:{f}", f"grep:a:{f}", f"wc:{f}", f"stat:{f}"):
        assert "[branch:" not in supertool.dispatch(arg), arg


def test_no_footer_when_there_is_no_branch(tmp_path: Path, monkeypatch) -> None:
    """Outside a repo the footer is absent, not `[branch: ]`."""
    monkeypatch.setattr(supertool, "_branch_reading", lambda: ("", ""))
    f = tmp_path / "x.py"
    f.write_text("a = 1\n")
    out = supertool.dispatch(f"edit:::a = 1:::a = 2:::{f}")
    assert "[branch:" not in out


def test_batch_emits_one_footer_not_one_per_sub_op(tmp_path: Path, monkeypatch) -> None:
    """A batch runs each sub-op through dispatch recursively — unguarded, a
    50-edit batch would print the branch 50 times."""
    monkeypatch.setattr(supertool, "_branch_reading", lambda: ("my-feature", ""))
    f = tmp_path / "x.py"
    f.write_text("a\nb\nc\n")
    payload = tmp_path / "ops.json"
    payload.write_text(json.dumps([
        {"op": "edit", "path": str(f), "old": "a", "new": "A"},
        {"op": "edit", "path": str(f), "old": "b", "new": "B"},
        {"op": "edit", "path": str(f), "old": "c", "new": "C"},
    ]))
    out = supertool.dispatch(f"batch:@{payload}")
    assert out.count("[branch: my-feature]") == 1
    assert out.rstrip().endswith("[branch: my-feature]")


def test_read_only_batch_has_no_footer(tmp_path: Path, monkeypatch) -> None:
    """Nothing was written, so there is no branch worth reporting."""
    monkeypatch.setattr(supertool, "_branch_reading", lambda: ("my-feature", ""))
    f = tmp_path / "x.py"
    f.write_text("a\n")
    payload = tmp_path / "ops.json"
    payload.write_text(json.dumps([{"op": "read", "path": str(f)}]))
    out = supertool.dispatch(f"batch:@{payload}")
    assert "[branch:" not in out


def test_nested_batch_still_reports_the_branch(tmp_path: Path, monkeypatch) -> None:
    """#392: a mutation buried in an inner batch used to produce ZERO footers —
    the inner one suppressed by depth, the outer one blind because its only
    sub-op was "batch", which is not in _OP_TARGETS."""
    monkeypatch.setattr(supertool, "_branch_reading", lambda: ("my-feature", ""))
    f = tmp_path / "x.py"
    f.write_text("a = 1\\n")
    inner = tmp_path / "inner.json"
    inner.write_text(json.dumps([
        {"op": "edit", "path": str(f), "old": "a = 1", "new": "a = 2"},
    ]))
    outer = tmp_path / "outer.json"
    outer.write_text(json.dumps([{"op": "batch", "path": f"@{inner}"}]))
    out = supertool.dispatch(f"batch:@{outer}")
    assert f.read_text(encoding="utf-8") == "a = 2\\n"
    assert out.count("[branch: my-feature]") == 1


def test_append_gets_the_footer_too(tmp_path: Path, monkeypatch) -> None:
    """"Every mutating op" has to mean every one — a silently excluded op is
    exactly the wrong-branch miss #381 exists to close."""
    monkeypatch.setattr(supertool, "_branch_reading", lambda: ("my-feature", ""))
    f = tmp_path / "x.py"
    f.write_text("a\n")
    out = supertool.dispatch(f"append:::{f}:::b")
    assert "[branch: my-feature]" in out


def test_replace_dry_stays_footer_free(tmp_path: Path, monkeypatch) -> None:
    """A preview op writes nothing, so there is no branch to warn about."""
    monkeypatch.setattr(supertool, "_branch_reading", lambda: ("my-feature", ""))
    f = tmp_path / "x.py"
    f.write_text("a = 1\n")
    out = supertool.dispatch(f"replace_dry:::a = 1:::a = 2:::{f}")
    assert "[branch:" not in out


# ---------------------------------------------------------------------------
# edit-miss diagnostics
# ---------------------------------------------------------------------------

def test_diagnostic_marker_is_ascii_in_plain_mode(tmp_path: Path, monkeypatch) -> None:
    """plain_mode exists so a C/POSIX-locale grep can match the output — a new
    message must not reintroduce a multibyte glyph it was meant to strip."""
    monkeypatch.setenv("SUPERTOOL_PLAIN", "1")
    f = tmp_path / "x.py"
    f.write_text("alpha = 1\n")
    out = supertool.op_edit("alpha = 11", "q", str(f))
    assert "↳" not in out
    assert "  -> nearest match" in out

def test_doubled_backslash_is_named(tmp_path: Path) -> None:
    """TOML literal strings don't process escapes, so `\\\\302` in a payload is
    two literal backslashes and can never match a file holding `\\302`."""
    f = tmp_path / "x.py"
    f.write_text('BAR = "\\302"\n')
    out = supertool.op_edit('BAR = "\\\\302"', 'BAR = "x"', str(f))
    assert "ERROR: old string not found" in out
    assert "SINGLE backslashes" in out
    assert "do not process escapes" in out


def test_whitespace_only_difference_is_named(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("def foo():\n        return bar(1)\n")
    out = supertool.op_edit("return  bar(1)", "return bar(2)", str(f))
    assert "line 2 matches ignoring whitespace" in out
    assert "check indentation" in out


def test_nearest_line_is_reported(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("alpha = 1\nbeta = 2\ngamma = 3\n")
    out = supertool.op_edit("beta = 22", "beta = 3", str(f))
    assert "nearest match at line 2" in out
    assert "beta = 2" in out


def test_no_diagnostic_when_nothing_is_close(tmp_path: Path) -> None:
    """A wild miss gets no invented suggestion."""
    f = tmp_path / "x.py"
    f.write_text("alpha = 1\n")
    out = supertool.op_edit("zzzzzzzzzzzzzzzzzz", "q", str(f))
    assert "ERROR: old string not found" in out
    assert "↳" not in out


def test_diagnostic_skipped_on_a_huge_file(tmp_path: Path, monkeypatch) -> None:
    """The scan is a courtesy on a failure path — it must never become the
    slow part of a failed edit."""
    monkeypatch.setattr(supertool, "_EDIT_DIAG_MAX_LINES", 5)
    f = tmp_path / "x.py"
    f.write_text("".join(f"line{i}\n" for i in range(20)))
    out = supertool.op_edit("line3x", "q", str(f))
    assert "ERROR: old string not found" in out
    assert "nearest match" not in out


def test_backslash_hint_still_fires_on_a_huge_file(tmp_path: Path, monkeypatch) -> None:
    """It's a substring check, not a scan — the size cap doesn't apply."""
    monkeypatch.setattr(supertool, "_EDIT_DIAG_MAX_LINES", 5)
    f = tmp_path / "x.py"
    f.write_text("".join(f"line{i}\n" for i in range(20)) + 'B = "\\302"\n')
    out = supertool.op_edit('B = "\\\\302"', "q", str(f))
    assert "SINGLE backslashes" in out


def test_ambiguous_match_keeps_its_own_error(tmp_path: Path) -> None:
    """>1 match is a different failure — it must not gain a 'not found' hint."""
    f = tmp_path / "x.py"
    f.write_text("a = 1\na = 1\n")
    out = supertool.op_edit("a = 1", "a = 2", str(f))
    assert "ambiguous" in out
    assert "↳" not in out


def test_successful_edit_has_no_diagnostic(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("a = 1\n")
    out = supertool.op_edit("a = 1", "a = 2", str(f))
    assert "↳" not in out
