"""Unit tests for presets/git/checkout.py."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from typing import Any


PRESET = Path(__file__).parent.parent / "presets" / "git" / "checkout.py"
_spec = importlib.util.spec_from_file_location("git_checkout", PRESET)
assert _spec is not None and _spec.loader is not None
checkout = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checkout)


def _fake_run(stdout: str = "", stderr: str = "", returncode: int = 0) -> Any:
    return subprocess.CompletedProcess(
        args=["git"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_no_arg_prints_usage(monkeypatch, capsys) -> None:
    monkeypatch.setattr(checkout.sys, "argv", ["checkout.py"])
    rc = checkout.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "usage" in out


# Four fixtures used to sit here, covering checkout.py's three recovery paths.
# Each handed `_git` git's English error sentence and then asserted the code
# parsed that sentence — so they pinned the implementation rather than the
# behaviour. git translates that sentence, so they passed identically in every
# locale while the recoveries they claimed to cover fired only for an English
# git (#649). Replaced by real-artifact, locale-parametrised reproductions in
# test_git_checkout_recovery_649.py, which assert where the caller ends up and
# name no git message at all.
#
# The worktree-lock fixture below is deliberately kept as a mock. It pins a
# message, not a state change — nothing is rewritten on that path — and the
# scenario (a ref held by a second worktree) is the one case here where the
# arrangement costs more to build than the assertion is worth. Its English
# dependency is real and documented in the same issue.


def test_checkout_worktree_locked_suggests_path(monkeypatch, capsys) -> None:
    """When ref is checked out in another worktree, suggest cd <path>."""
    err = (
        "fatal: 'feature/x' is already used by worktree at "
        "'/private/tmp/dvsi-google-tests'"
    )

    def fake(args, timeout=10):
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return _fake_run("master\n")
        if args[:2] == ["rev-parse", "--short"]:
            return _fake_run("abc1234\n")
        if args[0] == "checkout":
            return _fake_run("", err, 128)
        return _fake_run()

    monkeypatch.setattr(checkout, "_git", fake)
    monkeypatch.setattr(checkout.sys, "argv", ["checkout.py", "feature/x"])
    rc = checkout.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "another worktree" in out
    assert "/private/tmp/dvsi-google-tests" in out
    assert "cd /private/tmp/dvsi-google-tests" in out
    assert "git worktree remove" in out
