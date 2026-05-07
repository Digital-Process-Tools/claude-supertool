"""Unit tests for presets/git/resolve.py — argument validation."""
from __future__ import annotations

import importlib.util
from pathlib import Path


PRESET = Path(__file__).parent.parent / "presets" / "git" / "resolve.py"
_spec = importlib.util.spec_from_file_location("git_resolve", PRESET)
assert _spec is not None and _spec.loader is not None
resolve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(resolve)


def test_missing_args_prints_usage(monkeypatch, capsys) -> None:
    monkeypatch.setattr(resolve.sys, "argv", ["resolve.py", "ours"])
    rc = resolve.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "usage" in out


def test_invalid_side_rejected(monkeypatch, capsys) -> None:
    monkeypatch.setattr(resolve.sys, "argv", ["resolve.py", "mine", "x.py"])
    rc = resolve.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "must be 'ours' or 'theirs'" in out


def test_side_case_insensitive(monkeypatch, capsys) -> None:
    """SIDE accepts uppercase."""
    import subprocess
    fake = subprocess.CompletedProcess(args=["git"], returncode=1, stdout="", stderr="not a git repo")
    monkeypatch.setattr(resolve, "_git", lambda args, timeout=10: fake)
    monkeypatch.setattr(resolve.sys, "argv", ["resolve.py", "OURS", "x.py"])
    rc = resolve.main()
    # Reaches the "not in a git repo" check — proves SIDE was accepted
    out = capsys.readouterr().out
    assert "not inside a git repository" in out
    assert rc == 1
