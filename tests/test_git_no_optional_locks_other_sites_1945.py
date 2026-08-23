"""#1945 — dashboard.py and pr_merge.py's own `_git` chokepoints, same flag.

Sibling instance of #1944/#1945's `_git_common._git`: both files define their
own thin `_git()` wrapper rather than importing the shared one, so the fix
has to land at each wrapper separately. `dashboard._git`'s own docstring
already says "for a read-only git command" -- the flag matches what it
claims. `pr_merge._git` is the chokepoint for both reads (`rev-parse`,
`worktree list`, `status`, `ls-files`) and writes (`branch -d`, `worktree
remove`) it runs, and `--no-optional-locks` is a documented no-op on the
latter -- verified against real git 2.46.2 for both commands.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent


def _load(rel: str, name: str):
    path = REPO / rel
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_dashboard_git_puts_the_flag_right_after_the_binary(monkeypatch) -> None:
    """MUST NOT FIRE."""
    dashboard = _load("presets/dashboard/dashboard.py", "dashboard_1945")
    calls: list = []

    def _fake_run(cmd, **kw):
        calls.append(list(cmd))
        return mock.Mock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(dashboard, "_run", _fake_run)
    dashboard._git(["status", "--porcelain"])
    assert len(calls) == 1, calls
    assert calls[0][0] == "git", calls
    assert calls[0][1] == "--no-optional-locks", calls
    assert calls[0][2:] == ["status", "--porcelain"], calls


def test_pr_merge_git_puts_the_flag_right_after_the_binary(monkeypatch) -> None:
    """MUST NOT FIRE."""
    pr_merge = _load("presets/github/pr_merge.py", "pr_merge_1945")
    calls: list = []

    def _fake_run(cmd, **kw):
        calls.append(list(cmd))
        return mock.Mock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(pr_merge.subprocess, "run", _fake_run)
    pr_merge._git(["rev-parse", "--short", "HEAD"])
    assert len(calls) == 1, calls
    assert calls[0][0] == "git", calls
    assert calls[0][1] == "--no-optional-locks", calls
    assert calls[0][2:] == ["rev-parse", "--short", "HEAD"], calls


def test_pr_merge_git_still_runs_a_real_write_command() -> None:
    """MUST FIRE. Harm-check: `_git` is also the chokepoint for `branch -d`."""
    import subprocess as _subprocess
    pr_merge = _load("presets/github/pr_merge.py", "pr_merge_1945_write")
    # Unmocked: a nonexistent branch name still reaches git and gets a real,
    # well-formed refusal rather than the flag itself breaking dispatch.
    result = pr_merge._git(["branch", "-d", "definitely-not-a-real-branch-1945"])
    assert isinstance(result, _subprocess.CompletedProcess)
    assert result.returncode != 0
    assert "branch" in (result.stderr or result.stdout).lower()
