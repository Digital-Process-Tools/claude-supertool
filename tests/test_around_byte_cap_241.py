"""Regression tests for #241 — around:/grep_around: per-op byte cap.

A large context (:N) on a file of long (minified) lines could dump hundreds
of KB in a single op, blowing the caller's context budget. The ops now cap
their output at a byte budget, truncating at a line boundary and appending a
footer that points at the narrower tools (smaller :N, between:).
"""
from __future__ import annotations

from pathlib import Path

import supertool


# A long single line so a few of them blow past the 16KB cap fast.
LONG = "x" * 4000


def _make_long_file(tmp_path: Path, n_lines: int = 40) -> Path:
    f = tmp_path / "big.js"
    body = "\n".join(f"line{i} {LONG} TARGET{i}" for i in range(n_lines)) + "\n"
    f.write_text(body)
    return f


def test_around_caps_large_window(tmp_path: Path) -> None:
    f = _make_long_file(tmp_path)
    out = supertool.op_around("TARGET20", str(f), 40)
    assert len(out.encode()) <= 16000 + 200, f"not capped: {len(out)} bytes"
    assert "truncated" in out
    assert "between:" in out


def test_around_truncates_at_line_boundary(tmp_path: Path) -> None:
    f = _make_long_file(tmp_path)
    out = supertool.op_around("TARGET20", str(f), 40)
    # Everything before the footer must end on a complete line (newline).
    footer_idx = out.index("… truncated")
    body = out[:footer_idx]
    assert body.endswith("\n"), "truncation cut mid-line"


def test_around_small_file_not_capped(tmp_path: Path) -> None:
    f = tmp_path / "small.py"
    f.write_text("a\nb\nTARGET\nd\ne\n")
    out = supertool.op_around("TARGET", str(f), 2)
    assert "truncated" not in out
    assert "TARGET" in out


def test_grep_around_caps_large_window(tmp_path: Path) -> None:
    f = _make_long_file(tmp_path)
    # grep_around routes through op_grep with context > 0.
    out = supertool.op_grep("TARGET", str(f), limit=50, context=40)
    assert len(out.encode()) <= 16000 + 200, f"not capped: {len(out)} bytes"
    assert "truncated" in out


def test_grep_no_context_not_capped_by_window(tmp_path: Path) -> None:
    """Plain grep (context=0) takes a different branch — no window footer."""
    f = tmp_path / "small.py"
    f.write_text("alpha\nTARGET here\nbeta\n")
    out = supertool.op_grep("TARGET", str(f), limit=10, context=0)
    assert "truncated (~" not in out
    assert "TARGET" in out


def test_around_env_override(tmp_path: Path, monkeypatch) -> None:
    f = _make_long_file(tmp_path)
    monkeypatch.setenv("SUPERTOOL_AROUND_MAX_BYTES", "4000")
    out = supertool.op_around("TARGET20", str(f), 40)
    assert len(out.encode()) <= 4000 + 200, f"env cap ignored: {len(out)} bytes"
    assert "truncated" in out
