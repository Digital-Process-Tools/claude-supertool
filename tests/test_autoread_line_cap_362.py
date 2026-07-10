"""Auto-read line-count cap (#362).

glob:/grep: auto-read a single matched file when it's under the byte cap.
Before #362 that gate was byte-only: a file well under ~20KB but with many
lines (e.g. a 187-line class) got fully dumped, overshooting context.

The fix mirrors the existing byte cap with a line-count cap: a single match
above the line threshold is NOT auto-dumped; instead the user gets the path
and the same manual-read hint. Small multi-line files still auto-read.
"""

from __future__ import annotations

from pathlib import Path

import supertool


def _many_lines_under_byte_cap(n: int) -> str:
    # n short lines: line count is high but total bytes stay well under the
    # 20KB byte cap, so the byte gate alone would let this through.
    return "".join(f"x{i} = {i}\n" for i in range(n))


def test_glob_no_auto_read_when_over_line_cap(tmp_path: Path, monkeypatch) -> None:
    body = _many_lines_under_byte_cap(200)
    assert len(body.encode()) < supertool.MAX_READ_BYTES  # under byte cap
    (tmp_path / "big.py").write_text(body)
    monkeypatch.chdir(tmp_path)
    out = supertool.op_glob("*.py")
    assert "[auto-read: glob returned 1 file]" not in out
    # body content must not be dumped
    assert "x150 = 150" not in out


def test_grep_no_auto_read_when_over_line_cap(tmp_path: Path) -> None:
    body = _many_lines_under_byte_cap(200)
    assert len(body.encode()) < supertool.MAX_READ_BYTES
    f = tmp_path / "big.py"
    f.write_text(body)
    out = supertool.op_grep("x1", str(f))
    assert "auto-read" not in out.lower() or "match found]" not in out
    assert "x150 = 150" not in out


def test_glob_still_auto_reads_small_multiline_file(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "small.py").write_text("a = 1\nb = 2\nc = 3\n")
    monkeypatch.chdir(tmp_path)
    out = supertool.op_glob("*.py")
    assert "[auto-read: glob returned 1 file]" in out
    assert "b = 2" in out


def test_grep_still_auto_reads_small_multiline_file(tmp_path: Path) -> None:
    f = tmp_path / "small.py"
    f.write_text("a = 1\nfindme = 2\nc = 3\n")
    out = supertool.op_grep("findme", str(f))
    assert "match found]" in out
    assert "c = 3" in out


def test_over_line_cap_shows_manual_read_hint(tmp_path: Path, monkeypatch) -> None:
    body = _many_lines_under_byte_cap(200)
    (tmp_path / "big.py").write_text(body)
    monkeypatch.chdir(tmp_path)
    out = supertool.op_glob("*.py")
    # user should still learn how to read it manually
    assert "read:" in out or ":no-auto-read" in out or "line" in out.lower()
