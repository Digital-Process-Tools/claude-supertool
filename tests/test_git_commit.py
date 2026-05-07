"""Unit tests for presets/git/commit.py — error parsing + empty-msg guard."""
from __future__ import annotations

import importlib.util
from pathlib import Path


PRESET = Path(__file__).parent.parent / "presets" / "git" / "commit.py"
_spec = importlib.util.spec_from_file_location("git_commit", PRESET)
assert _spec is not None and _spec.loader is not None
commit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(commit)


def test_first_error_line_picks_error_keyword() -> None:
    text = "Running pre-commit\nok 12 files\nfatal: hook rejected\nbye"
    assert commit._first_error_line(text) == "fatal: hook rejected"


def test_first_error_line_picks_emoji_marker() -> None:
    text = "step 1\nstep 2\n❌ Push blocked. Fix violations\n"
    assert "❌" in commit._first_error_line(text)


def test_first_error_line_falls_back_to_last_nonempty() -> None:
    assert commit._first_error_line("a\nb\n\n") == "b"


def test_first_error_line_empty_input() -> None:
    assert commit._first_error_line("") == ""
    assert commit._first_error_line("\n\n") == ""


def test_empty_message_rejected(monkeypatch, capsys) -> None:
    monkeypatch.setattr(commit.sys, "argv", ["commit.py", "   "])
    rc = commit.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "empty" in out


def test_no_args_prints_usage(monkeypatch, capsys) -> None:
    monkeypatch.setattr(commit.sys, "argv", ["commit.py"])
    rc = commit.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "usage" in out
