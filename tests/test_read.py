from __future__ import annotations

import sys
from pathlib import Path

import pytest

import _symlink
import supertool


def test_read_returns_line_numbered_content(tmp_path: Path) -> None:
    f = tmp_path / "hello.py"
    # write_bytes preserves LF — write_text on Windows translates \n → \r\n,
    # bumping byte count and adding a "crlf" meta tag.
    f.write_bytes(b"line1\nline2\nline3\n")
    out = supertool.op_read(str(f))
    assert "(3 lines, 18 bytes)" in out
    assert "     1→line1" in out
    assert "     3→line3" in out


def test_read_missing_file_returns_error(tmp_path: Path) -> None:
    out = supertool.op_read(str(tmp_path / "nope.py"))
    assert "ERROR: file not found" in out


def test_read_empty_path_returns_error() -> None:
    out = supertool.op_read("")
    assert "ERROR: file not found" in out


def test_read_complete_file_marker(tmp_path: Path) -> None:
    f = tmp_path / "small.py"
    f.write_text("x = 1\n")
    out = supertool.op_read(str(f))
    assert "[complete file — no more lines]" in out


def test_read_no_complete_marker_when_truncated(tmp_path: Path) -> None:
    f = tmp_path / "many.txt"
    f.write_text("\n".join(f"line{i}" for i in range(1, 11)) + "\n")
    out = supertool.op_read(str(f), offset=0, limit=3)
    assert "[complete file" not in out
    assert "more lines" in out


def test_read_with_offset_and_limit(tmp_path: Path) -> None:
    f = tmp_path / "many.txt"
    f.write_text("\n".join(f"line{i}" for i in range(1, 11)) + "\n")
    out = supertool.op_read(str(f), offset=3, limit=2)
    assert "     4→line4" in out
    assert "     5→line5" in out
    assert "line3" not in out
    assert "line6" not in out


def test_read_truncates_at_byte_cap(tmp_path: Path) -> None:
    f = tmp_path / "big.txt"
    # Write more than 20KB of content: 500 lines × ~100 chars = ~50KB
    f.write_text(("x" * 100 + "\n") * 500)
    out = supertool.op_read(str(f))
    assert "truncated at" in out
    assert "20000 bytes" in out


def test_read_reports_more_lines_available(tmp_path: Path) -> None:
    f = tmp_path / "long.txt"
    f.write_text("\n".join(f"line{i}" for i in range(1, 51)) + "\n")
    out = supertool.op_read(str(f), offset=0, limit=10)
    assert "more lines" in out


def test_read_directory_returns_error(tmp_path: Path) -> None:
    out = supertool.op_read(str(tmp_path))
    assert "ERROR: file not found" in out


def test_read_grep_filter(tmp_path: Path) -> None:
    f = tmp_path / "code.php"
    f.write_text("<?php\nuse Foo;\nuse Bar;\nclass X {\n}\n")
    out = supertool.op_read(str(f), grep_filter="use")
    assert "use Foo" in out
    assert "use Bar" in out
    assert "class X" not in out


def test_read_grep_filter_preserves_line_numbers(tmp_path: Path) -> None:
    f = tmp_path / "code.php"
    f.write_text("line1\nline2\ntarget\nline4\n")
    out = supertool.op_read(str(f), grep_filter="target")
    assert "3→target" in out


def test_read_grep_filter_no_matches(tmp_path: Path) -> None:
    f = tmp_path / "code.php"
    f.write_text("hello\nworld\n")
    out = supertool.op_read(str(f), grep_filter="ZZZZ")
    assert "no lines matching" in out


def test_read_grep_filter_with_offset(tmp_path: Path) -> None:
    f = tmp_path / "big.txt"
    lines = [f"line{i}\n" for i in range(20)]
    f.write_text("".join(lines))
    out = supertool.op_read(str(f), offset=5, limit=10, grep_filter="line1")
    # only lines 6-15 searched, line10-line14 match "line1"
    assert "line10" in out
    assert "line0" not in out


# ---------------------------------------------------------------------------
# render_file edge cases (shared helper)
# ---------------------------------------------------------------------------

def test_render_file_handles_binary_gracefully(tmp_path: Path) -> None:
    f = tmp_path / "bin.dat"
    f.write_bytes(b"\x00\x01\x02\xff\n")
    out = supertool.render_file(str(f))
    # Should not raise, should emit something
    assert "     1→" in out


def test_render_file_handles_empty_file(tmp_path: Path) -> None:
    f = tmp_path / "empty.txt"
    f.write_text("")
    out = supertool.render_file(str(f))
    assert "(0 lines, 0 bytes)" in out


# ---------------------------------------------------------------------------
# Abstract mode — size threshold, :full bypass, env var override
#
# Enabled here through the legacy `php_abstract` key on purpose: it is public
# API (#670) and these tests are what keeps it honoured. The threshold is set
# directly rather than through `max_bytes`, because since #670 `max_bytes` is
# also the yardstick the map has to beat — driving both from one knob made a
# 100-byte cap mean "abstract everything, then reject every map".
# ---------------------------------------------------------------------------

def _enable_abstract(monkeypatch, threshold: int = 100) -> None:
    monkeypatch.setattr(
        supertool, "_CONFIG",
        {"builtin-ops": {"read": {"php_abstract": 1,
                                  "abstract_threshold_bytes": threshold}}},
    )


def test_read_php_abstract_off_by_default(tmp_path: Path) -> None:
    f = tmp_path / "x.php"
    f.write_text("<?php\n" + "// x\n" * 200)
    out = supertool.op_read(str(f))
    assert "[abstract read" not in out
    assert "<?php" in out


def test_read_php_abstract_skipped_below_threshold(
    tmp_path: Path, monkeypatch
) -> None:
    _enable_abstract(monkeypatch, threshold=100000)
    f = tmp_path / "x.php"
    f.write_text("<?php\necho 'hi';\n")
    out = supertool.op_read(str(f))
    assert "[abstract read" not in out
    assert "echo" in out


def test_read_php_abstract_triggers_above_threshold(
    tmp_path: Path, monkeypatch
) -> None:
    _enable_abstract(monkeypatch, threshold=100)
    f = tmp_path / "x.php"
    f.write_text("<?php\nclass Foo {}\n" + "// pad\n" * 200)
    out = supertool.op_read(str(f))
    assert "[abstract read — php" in out
    assert "bytes raw" in out
    assert ":full for content" in out


def test_read_force_full_bypasses_abstract(
    tmp_path: Path, monkeypatch
) -> None:
    _enable_abstract(monkeypatch, threshold=100)
    f = tmp_path / "x.php"
    f.write_text("<?php\nclass Foo {}\n" + "// pad\n" * 200)
    out = supertool.op_read(str(f), force_full=True)
    assert "[abstract read" not in out
    assert "<?php" in out


def test_read_env_var_overrides_threshold(
    tmp_path: Path, monkeypatch
) -> None:
    _enable_abstract(monkeypatch, threshold=100000)
    monkeypatch.setenv("SUPERTOOL_READ_ABSTRACT_THRESHOLD_BYTES", "100")
    f = tmp_path / "x.php"
    f.write_text("<?php\nclass Foo {}\n" + "// pad\n" * 200)
    out = supertool.op_read(str(f))
    assert "[abstract read — php" in out


def test_dispatch_read_full_keyword(tmp_path: Path, monkeypatch) -> None:
    _enable_abstract(monkeypatch, threshold=100)
    f = tmp_path / "x.php"
    f.write_text("<?php\nclass Foo {}\n" + "// pad\n" * 200)
    out = supertool.dispatch(f"read:{f}:full")
    assert "[abstract read" not in out
    assert "<?php" in out


# ---------------------------------------------------------------------------
# Meta suffix (symlink / git / encoding / mtime / exec / crlf / conflict)
# ---------------------------------------------------------------------------

# NOT `_symlink.requires_symlink` (#1143), and the distinction is the point:
# every other symlink skip in this suite was a *capability* claim and is now
# probed, so it runs wherever the privilege exists -- including Windows. This one
# is not about a privilege at all. `op_read` renders the link target, and Windows
# spells a resolved target through a UNC prefix, so the assertion below is what
# is platform-specific, not the fixture. Converting it would make the test run on
# Windows and fail on its own assertion. `PLATFORM_SEMANTICS` declares that out
# loud so the guard test can tell the two apart instead of exempting it silently;
# making this assert something true on Windows is its own piece of work.
@pytest.mark.skipif(
    sys.platform == "win32",
    reason=_symlink.PLATFORM_SEMANTICS + "op_read renders the link target and "
    "Windows resolves it through a UNC prefix; this assertion expects a "
    "POSIX-style relative or absolute target.",
)
def test_read_meta_symlink(tmp_path: Path) -> None:
    target = tmp_path / "real.txt"
    target.write_text("hi")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    out = supertool.op_read(str(link))
    assert "->real.txt" in out or "->" + str(target) in out


def test_read_meta_binary(tmp_path: Path) -> None:
    f = tmp_path / "blob.bin"
    f.write_bytes(b"\x00\x01\x02\x03binary")
    out = supertool.op_read(str(f))
    assert " bin" in out.splitlines()[0]


def test_read_meta_crlf(tmp_path: Path) -> None:
    f = tmp_path / "win.txt"
    f.write_bytes(b"line1\r\nline2\r\n")
    out = supertool.op_read(str(f))
    assert "crlf" in out.splitlines()[0]


def test_read_meta_conflict_markers(tmp_path: Path) -> None:
    f = tmp_path / "conflicted.txt"
    f.write_text("ok\n<<<<<<< HEAD\nmine\n=======\ntheirs\n>>>>>>> branch\n")
    out = supertool.op_read(str(f))
    assert "cf!" in out.splitlines()[0]


def test_read_meta_clean_tracked_is_silent(tmp_path: Path) -> None:
    f = tmp_path / "plain.txt"
    # write_bytes preserves LF — write_text on Windows would emit \r\n and
    # trigger the "crlf" meta tag this test asserts is absent.
    f.write_bytes(b"hello\n")
    out = supertool.op_read(str(f))
    first = out.splitlines()[0]
    for tok in ("bin", "non-utf8", "crlf", "cf!", "->"):
        assert tok not in first


# ---------------------------------------------------------------------------
# _path_meta_suffix direct unit tests (coverage for branches not exercised
# via op_read end-to-end: exec bit, mtime, git ignored/modified, broken sym).
# ---------------------------------------------------------------------------

import os as _os
import subprocess as _sp
import time as _time


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows filesystem doesn't expose Unix executable bit — chmod 0o755 is a no-op.",
)
def test_path_meta_suffix_executable_bit(tmp_path: Path) -> None:
    f = tmp_path / "run.sh"
    f.write_text("#!/bin/sh\necho hi\n")
    _os.chmod(f, 0o755)
    out = supertool._path_meta_suffix(str(f), b"#!/bin/sh\n")
    assert " x" in out


def test_path_meta_suffix_stale_mtime_days(tmp_path: Path) -> None:
    f = tmp_path / "old.txt"
    f.write_text("ancient")
    old = _time.time() - 10 * 86400
    _os.utime(f, (old, old))
    out = supertool._path_meta_suffix(str(f), b"ancient")
    assert "10d" in out


def test_path_meta_suffix_stale_mtime_weeks(tmp_path: Path) -> None:
    f = tmp_path / "weeks.txt"
    f.write_text("middle")
    old = _time.time() - 60 * 86400
    _os.utime(f, (old, old))
    out = supertool._path_meta_suffix(str(f), b"middle")
    assert "8w" in out  # 60 // 7 = 8


def test_path_meta_suffix_stale_mtime_months(tmp_path: Path) -> None:
    f = tmp_path / "ancient.txt"
    f.write_text("old")
    old = _time.time() - 400 * 86400
    _os.utime(f, (old, old))
    out = supertool._path_meta_suffix(str(f), b"old")
    assert "mo" in out


def test_path_meta_suffix_broken_symlink(tmp_path: Path) -> None:
    _symlink.require_symlink()
    link = tmp_path / "dangling"
    link.symlink_to(tmp_path / "nope")
    out = supertool._path_meta_suffix(str(link))
    assert "broken" in out


def test_path_meta_suffix_git_modified(tmp_path: Path) -> None:
    cwd = _os.getcwd()
    _os.chdir(tmp_path)
    try:
        _sp.run(["git", "init", "-q"], check=True)
        _sp.run(["git", "config", "user.email", "t@t"], check=True)
        _sp.run(["git", "config", "user.name", "t"], check=True)
        f = tmp_path / "tracked.txt"
        f.write_text("orig\n")
        _sp.run(["git", "add", "tracked.txt"], check=True)
        _sp.run(["git", "commit", "-qm", "x"], check=True)
        f.write_text("modified\n")
        out = supertool._path_meta_suffix(str(f), b"modified\n")
        assert " m" in out
    finally:
        _os.chdir(cwd)


def test_path_meta_suffix_git_ignored(tmp_path: Path) -> None:
    cwd = _os.getcwd()
    _os.chdir(tmp_path)
    try:
        _sp.run(["git", "init", "-q"], check=True)
        (tmp_path / ".gitignore").write_text("secret.txt\n")
        f = tmp_path / "secret.txt"
        f.write_text("hush")
        out = supertool._path_meta_suffix(str(f), b"hush")
        assert " !" in out
    finally:
        _os.chdir(cwd)


# --- #309: read→edit hint ----------------------------------------------------
# A supertool read is a Bash subprocess, so the harness Edit tool's
# must-Read-first gate rejects it. supertool's own edit op bypasses that gate,
# so a single-file read receipt carries a one-line nudge toward it. The hint is
# scoped to single-file reads — grep/glob multi-file branches don't get it.

_HINT = "no harness Read needed"


def test_read_appends_edit_hint(tmp_path: Path) -> None:
    f = tmp_path / "hello.py"
    f.write_bytes(b"line1\nline2\n")
    out = supertool.op_read(str(f))
    assert _HINT in out
    assert f"edit:::OLD:::NEW:::{f}" in out


def test_read_hint_absent_on_missing_file(tmp_path: Path) -> None:
    out = supertool.op_read(str(tmp_path / "nope.py"))
    assert _HINT not in out


def test_read_hint_absent_on_grep(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_bytes(b"needle here\nother\n")
    out = supertool.op_grep("needle", str(f))
    assert _HINT not in out


def test_read_hint_absent_on_glob(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"x\n")
    (tmp_path / "b.txt").write_bytes(b"y\n")
    out = supertool.op_glob(str(tmp_path / "*.txt"))
    assert _HINT not in out
