"""Unit tests for presets/git/checkout.py."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from typing import Any

import pytest


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


def test_unknown_ref_auto_fetches_then_errors(monkeypatch, capsys) -> None:
    """Pathspec error triggers auto-fetch; if ref still missing, actionable error."""
    calls: list[list[str]] = []

    def fake(args, timeout=10):
        calls.append(args)
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return _fake_run("master\n")
        if args[:2] == ["rev-parse", "--short"]:
            return _fake_run("abc1234\n")
        if args[0] == "checkout":
            return _fake_run("", "error: pathspec 'nope' did not match any file(s)", 1)
        if args[0] == "fetch":
            return _fake_run()
        return _fake_run()

    monkeypatch.setattr(checkout, "_git", fake)
    monkeypatch.setattr(checkout.sys, "argv", ["checkout.py", "nope"])
    rc = checkout.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert any(c[0] == "fetch" for c in calls), "should auto-fetch on pathspec error"
    assert "not found" in out
    assert "after fetch" in out


def test_unknown_ref_recovers_after_auto_fetch(monkeypatch, capsys) -> None:
    """If ref appears after fetch (e.g. just-pushed remote branch), checkout succeeds."""
    checkout_attempts = {"n": 0}

    def fake(args, timeout=10):
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return _fake_run("master\n" if checkout_attempts["n"] == 0 else "feature\n")
        if args[:2] == ["rev-parse", "--short"]:
            return _fake_run("abc1234\n")
        if args[0] == "checkout":
            checkout_attempts["n"] += 1
            if checkout_attempts["n"] == 1:
                return _fake_run("", "error: pathspec 'feature' did not match any file(s)", 1)
            return _fake_run("Switched to branch 'feature'\n")
        if args[0] == "fetch":
            return _fake_run()
        if args[:3] == ["rev-parse", "--abbrev-ref", "--symbolic-full-name"]:
            return _fake_run("", "", 1)
        return _fake_run()

    monkeypatch.setattr(checkout, "_git", fake)
    monkeypatch.setattr(checkout.sys, "argv", ["checkout.py", "feature"])
    rc = checkout.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "auto-fetched" in out
    assert "→ feature" in out


def test_narrowed_refspec_recovers_via_single_ref_fetch(monkeypatch, capsys) -> None:
    """#267: single-branch refspec → `fetch --all` never creates origin/<branch>,
    so the plain-checkout retry still fails. Falls back to an explicit
    `fetch origin <branch>` + `checkout -B <branch> FETCH_HEAD`."""
    calls: list[list[str]] = []
    on_feature = {"yet": False}

    def fake(args, timeout=10):
        calls.append(args)
        if args[:2] == ["rev-parse", "--abbrev-ref"] and "--symbolic-full-name" not in args:
            return _fake_run("feature\n" if on_feature["yet"] else "master\n")
        if args[:2] == ["rev-parse", "--short"]:
            return _fake_run("abc1234\n")
        if args[:3] == ["rev-parse", "--abbrev-ref", "--symbolic-full-name"]:
            return _fake_run("", "", 1)
        if args[0] == "checkout" and args[1] == "-B":
            on_feature["yet"] = True
            return _fake_run("Reset branch 'feature'\n")
        if args[0] == "checkout":
            return _fake_run("", "error: pathspec 'feature' did not match any file(s)", 1)
        if args[0] == "fetch":
            return _fake_run()
        return _fake_run()

    monkeypatch.setattr(checkout, "_git", fake)
    monkeypatch.setattr(checkout.sys, "argv", ["checkout.py", "feature"])
    rc = checkout.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert ["fetch", "origin", "feature"] in calls, "should fall back to single-ref fetch"
    assert ["checkout", "-B", "feature", "FETCH_HEAD"] in calls
    assert "FETCH_HEAD" in out
    assert "→ feature" in out


def test_narrowed_refspec_dirty_tree_reports_real_blocker(monkeypatch, capsys) -> None:
    """#267 fetch succeeds but `checkout -B <branch> FETCH_HEAD` is blocked by a
    dirty working tree. The error must name the real blocker (uncommitted
    changes), NOT the stale 'not found after fetch' from the first attempt."""
    calls: list[list[str]] = []

    def fake(args, timeout=10):
        calls.append(args)
        if args[:2] == ["rev-parse", "--abbrev-ref"] and "--symbolic-full-name" not in args:
            return _fake_run("master\n")
        if args[:2] == ["rev-parse", "--short"]:
            return _fake_run("abc1234\n")
        if args[:3] == ["rev-parse", "--abbrev-ref", "--symbolic-full-name"]:
            return _fake_run("", "", 1)
        if args[0] == "checkout" and args[1] == "-B":
            return _fake_run(
                "",
                "error: Your local changes to the following files would be "
                "overwritten by checkout:\n\tdocs/dvsi.sql\n"
                "Please commit your changes or stash them before you switch branches.",
                1,
            )
        if args[0] == "checkout":
            return _fake_run("", "error: pathspec 'feature' did not match any file(s)", 1)
        if args[0] == "fetch":
            return _fake_run()
        return _fake_run()

    monkeypatch.setattr(checkout, "_git", fake)
    monkeypatch.setattr(checkout.sys, "argv", ["checkout.py", "feature"])
    rc = checkout.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert ["checkout", "-B", "feature", "FETCH_HEAD"] in calls
    assert "uncommitted changes" in out.lower() or "stash" in out.lower()
    assert "not found even after fetch" not in out


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
