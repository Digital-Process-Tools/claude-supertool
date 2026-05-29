"""Regression tests for #259 — atomic write must preserve the file's mode.

The atomic-write path (`_atomic_write`) creates a temp file via mkstemp
(mode 0600) and renames it over the target. Before the fix, the original
file's mode was never copied onto the temp file, so an executable script
(0755) silently became 0600/0644 after any mutating op — breaking `cb.sh`,
git hooks, and any `*.sh` edited through supertool.

These tests pin: every mutating op preserves the original mode; a brand-new
file created by paste keeps the default mode (no spurious +x).
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import supertool


def _mode(p: Path) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def test_edit_preserves_executable_bit(tmp_path: Path) -> None:
    f = tmp_path / "cb.sh"
    f.write_text("echo X\n")
    os.chmod(f, 0o755)
    out = supertool.op_edit("echo X", "echo Y", str(f))
    assert "ERROR" not in out, out
    assert _mode(f) == 0o755, f"mode lost: {oct(_mode(f))}"


def test_paste_rewrite_preserves_executable_bit(tmp_path: Path) -> None:
    f = tmp_path / "hook.sh"
    f.write_text("old\n")
    os.chmod(f, 0o755)
    out = supertool.op_paste(str(f), "new content\n")
    assert "ERROR" not in out, out
    assert _mode(f) == 0o755, f"mode lost: {oct(_mode(f))}"


def test_replace_lines_preserves_executable_bit(tmp_path: Path) -> None:
    f = tmp_path / "run.sh"
    f.write_text("a\nb\nc\n")
    os.chmod(f, 0o755)
    out = supertool.op_replace_lines(str(f), 2, 2, "B\n")
    assert "ERROR" not in out, out
    assert _mode(f) == 0o755, f"mode lost: {oct(_mode(f))}"


def test_replace_preserves_executable_bit(tmp_path: Path) -> None:
    f = tmp_path / "deploy.sh"
    f.write_text("foo bar\n")
    os.chmod(f, 0o755)
    out = supertool.op_replace("foo", "baz", str(f))
    assert "ERROR" not in out, out
    assert _mode(f) == 0o755, f"mode lost: {oct(_mode(f))}"


def test_vim_preserves_executable_bit(tmp_path: Path) -> None:
    """vim is named in the issue and routes through _atomic_write too."""
    f = tmp_path / "macro.sh"
    f.write_text("echo X\n")
    os.chmod(f, 0o755)
    out = supertool.op_vim(str(f), ":s/X/Y/")
    assert "ERROR" not in out, out
    assert f.read_text() == "echo Y\n", "substitution must apply"
    assert _mode(f) == 0o755, f"mode lost: {oct(_mode(f))}"


def test_edit_preserves_nondefault_readonly_group(tmp_path: Path) -> None:
    """Any non-default mode survives — not just the executable bit."""
    f = tmp_path / "data.txt"
    f.write_text("X\n")
    os.chmod(f, 0o640)
    out = supertool.op_edit("X", "Y", str(f))
    assert "ERROR" not in out, out
    assert _mode(f) == 0o640, f"mode lost: {oct(_mode(f))}"


def test_paste_new_file_keeps_default_mode(tmp_path: Path) -> None:
    """A brand-new file must NOT inherit a spurious executable bit."""
    f = tmp_path / "fresh.txt"
    out = supertool.op_paste(str(f), "hello\n")
    assert "ERROR" not in out, out
    assert not (_mode(f) & 0o111), f"new file unexpectedly executable: {oct(_mode(f))}"
