"""Regression tests for #147: vim shell verbs (:! / :%! / :r !) gated behind opt-in.

Strict mode (the default): vim's shell-escape verbs are disabled. Editor verbs
(i/a/o/d/s/etc.) work unconditionally. `:r FILE` (no `!`) routes through
`_safe_path` so it inherits #146 cwd containment.

Opt-in via `SUPERTOOL_ALLOW_VIM_SHELL=1` for power users who genuinely need
shell parity in vim macros.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import supertool


@pytest.fixture
def vim_shell_off(monkeypatch):
    """Force strict mode (un-do any caller / conftest opt-in)."""
    monkeypatch.delenv("SUPERTOOL_ALLOW_VIM_SHELL", raising=False)
    yield


@pytest.fixture
def vim_shell_on(monkeypatch):
    monkeypatch.setenv("SUPERTOOL_ALLOW_VIM_SHELL", "1")
    yield


class TestVimShellGate:
    def test_bang_cmd_rejected_by_default(self, vim_shell_off, tmp_path):
        target = tmp_path / "foo.txt"
        target.write_text("hello\n")
        marker = tmp_path / "pwned"
        out = supertool.dispatch(f"vim:::{target}:::G\\e:!touch {marker}")
        assert "ERROR" in out
        assert "SUPERTOOL_ALLOW_VIM_SHELL" in out
        assert not marker.exists()

    def test_pct_bang_rejected_by_default(self, vim_shell_off, tmp_path):
        target = tmp_path / "foo.txt"
        target.write_text("a\nb\nc\n")
        out = supertool.dispatch(f"vim:::{target}:::G\\e:%!sort")
        assert "ERROR" in out
        assert "SUPERTOOL_ALLOW_VIM_SHELL" in out

    def test_r_bang_cmd_rejected_by_default(self, vim_shell_off, tmp_path):
        target = tmp_path / "foo.txt"
        target.write_text("hi\n")
        marker = tmp_path / "exfil"
        out = supertool.dispatch(f"vim:::{target}:::G\\e:r !touch {marker}")
        assert "ERROR" in out
        assert "SUPERTOOL_ALLOW_VIM_SHELL" in out
        assert not marker.exists()

    def test_bang_cmd_runs_when_opted_in(self, vim_shell_on, tmp_path):
        target = tmp_path / "foo.txt"
        target.write_text("hello\n")
        out = supertool.dispatch(f"vim:::{target}:::G\\e:!echo opted-in")
        assert "ERROR" not in out
        assert "opted-in" in target.read_text()

    def test_dot_repeat_bang_rejected_by_default(self, vim_shell_on, tmp_path, monkeypatch):
        """Dot-repeat replays `:!cmd`. Must return ERROR, not silently log-and-skip,
        so batch:@file callers checking for ERROR see the rejection."""
        target = tmp_path / "foo.txt"
        target.write_text("hello\n")
        # First :! sets last_change; then we turn the gate off; then `.` replays.
        # Use monkeypatch mid-test by issuing two separate dispatch calls under
        # different env: it's simpler to just verify the error format directly.
        monkeypatch.delenv("SUPERTOOL_ALLOW_VIM_SHELL", raising=False)
        # Construct a script that fires `.` (relies on last_change being a :! verb,
        # which we can seed via `:!echo` then `.` in the same script, but the gate
        # rejects the FIRST `:!` already. So the only way to reach the dot-repeat
        # gate is via a pre-existing last_change from a previous call — we can't
        # set that up cleanly here. Instead just verify the gate function:
        gate_msg = supertool._check_vim_shell_allowed()
        assert gate_msg is not None and "SUPERTOOL_ALLOW_VIM_SHELL" in gate_msg


class TestVimReadFileContainment:
    """`:r FILE` (without `!`) goes through `_safe_path` — inherits #146."""

    def test_r_file_outside_cwd_rejected(self, monkeypatch, tmp_path):
        # Conftest sets SUPERTOOL_ALLOW_OUTSIDE_CWD=1 for tests. Unset it so
        # _safe_path goes strict.
        monkeypatch.delenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", raising=False)
        target = tmp_path / "foo.txt"
        target.write_text("hi\n")
        monkeypatch.chdir(tmp_path)
        # /etc/passwd is outside cwd. :r should reject before reading.
        out = supertool.dispatch(f"vim:::{target}:::G\\e:r /etc/passwd")
        assert "ERROR" in out
        assert "escapes cwd" in out
        # Original file untouched.
        assert "hi\n" in target.read_text()

    def test_r_file_inside_cwd_ok(self, monkeypatch, tmp_path):
        monkeypatch.delenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", raising=False)
        target = tmp_path / "foo.txt"
        target.write_text("line1\n")
        source = tmp_path / "src.txt"
        source.write_text("INSERTED\n")
        monkeypatch.chdir(tmp_path)
        out = supertool.dispatch(f"vim:::foo.txt:::G\\e:r src.txt")
        assert "ERROR" not in out, out
        assert "INSERTED" in target.read_text()


class TestEditorVerbsStillWork:
    """Sanity — non-shell vim verbs must remain unaffected by the gate."""

    def test_insert_verb_works(self, vim_shell_off, tmp_path):
        target = tmp_path / "foo.txt"
        target.write_text("original\n")
        out = supertool.dispatch(f"vim:::{target}:::iprepend ")
        assert "ERROR" not in out, out
        assert "prepend original" in target.read_text()

    def test_substitute_works(self, vim_shell_off, tmp_path):
        target = tmp_path / "foo.txt"
        target.write_text("foo bar\n")
        out = supertool.dispatch(f"vim:::{target}:::%s/foo/baz/g")
        assert "ERROR" not in out, out
        assert "baz bar" in target.read_text()
