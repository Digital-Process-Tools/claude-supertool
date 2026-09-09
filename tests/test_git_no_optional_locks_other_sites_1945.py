"""#1945 — `--no-optional-locks` sits right after the binary, at the chokepoint.

This file used to test two `_git()` wrappers, one in
`presets/dashboard/dashboard.py` and one in `presets/github/pr_merge.py`,
because each built its own git argv and #1945's flag had to be propagated to
both by hand. Its own first paragraph said so: "the fix has to land at each
wrapper separately".

That is no longer true. #2447 moved the invocation to `presets/_git_run.py`
and both files now route through it, so there is one argv to pin rather than
three — and this file no longer reaches into two different preset directories
to do it, which is what made it span two lanes on the coupling checker.
`tests/test_git_invocation_chokepoint_2447.py` is what keeps a fourth wrapper
from reappearing; this file keeps the flag where the flag has to be.

`--no-optional-locks` is a git *global* flag, so it precedes the subcommand.
It is a documented no-op on a write command — verified against real git 2.46.2
for both `branch -d` and `worktree remove` — which is why `_stop()`'s SIGTERM
grace exists beside it rather than instead of it.
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load(rel: str, name: str):
    path = REPO / rel
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeProc:
    """Enough of `Popen` for `_git_attempt` to finish against."""

    returncode = 0

    def communicate(self, timeout=None):
        return ("ok", "")


def test_git_puts_the_flag_right_after_the_binary(monkeypatch) -> None:
    """MUST NOT FIRE."""
    run = _load("presets/_git_run.py", "git_run_1945")
    calls: list = []

    def _fake_popen(cmd, **kw):
        calls.append(list(cmd))
        return _FakeProc()

    monkeypatch.setattr(run.subprocess, "Popen", _fake_popen)
    run._git(["status", "--porcelain"])
    assert len(calls) == 1, calls
    assert calls[0][0] == "git", calls
    assert calls[0][1] == "--no-optional-locks", calls
    assert calls[0][2:] == ["status", "--porcelain"], calls


def test_git_verbatim_puts_the_flag_right_after_the_binary(monkeypatch) -> None:
    """MUST NOT FIRE. The second invocation shape carries the same flag."""
    run = _load("presets/_git_run.py", "git_run_1945_verbatim")
    calls: list = []

    class _FakeBytesProc(_FakeProc):
        def communicate(self, timeout=None):
            return (b"ok", b"")

    def _fake_popen(cmd, **kw):
        calls.append(list(cmd))
        return _FakeBytesProc()

    monkeypatch.setattr(run.subprocess, "Popen", _fake_popen)
    run._git_verbatim(["blame", "--line-porcelain", "README.md"])
    assert len(calls) == 1, calls
    assert calls[0][:2] == ["git", "--no-optional-locks"], calls


# The two assertions above rest on a recorded argv, and both guard the vacuous
# case with `len(calls) == 1` rather than slicing an empty list. Their positive
# control is the pair below: unmocked, against real git, through the whole
# route the two adopters now take. A control that only exercised the fake
# recorder would say nothing about either.


def test_pr_merge_git_still_runs_a_real_write_command() -> None:
    """MUST FIRE. Harm-check: `pr_merge._git` is the chokepoint for `branch -d`,
    and it now reaches git through `_git_run` rather than its own
    `subprocess.run`. Unmocked, so the whole route is exercised: a nonexistent
    branch name still gets a real, well-formed refusal rather than the move
    breaking dispatch."""
    pr_merge = _load("presets/github/pr_merge.py", "pr_merge_1945_write")
    result = pr_merge._git(["branch", "-d", "definitely-not-a-real-branch-1945"])
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode != 0
    assert "branch" in (result.stderr or result.stdout).lower()


def test_dashboard_git_still_answers_through_the_chokepoint() -> None:
    """MUST FIRE. The same harm-check on the read side, unmocked: `dashboard._git`
    renders `(stdout, error)` and has to keep doing so now that the invocation
    moved."""
    dashboard = _load("presets/dashboard/dashboard.py", "dashboard_1945_read")
    out, err = dashboard._git(["rev-parse", "--abbrev-ref", "HEAD"])
    assert err == "", err
    assert out.strip(), out
