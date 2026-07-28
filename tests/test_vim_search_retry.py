"""Hardening tests for vim `/PAT` and `?PAT` search retry autocorrects.

Background: PR #59 added BOF retry for forward search when the cursor was
persisted mid-file. Investigation for this PR found:

1. Trailing-slash retry (`/foo/` → `/foo`) did not chain into BOF retry —
   if the trimmed pattern also missed from cursor, BOF retry ran on the
   ORIGINAL still-slashed pattern. Fix: trailing-slash retry now also tries
   from BOF when cursor > 0.

2. Backward search `?PAT` had no parallel "retry from EOF" autocorrect.
   Persisted cursor at BOF + `?LATER` always errors despite the match
   existing later in the file. Fix: when `?PAT` misses and cursor is NOT
   already at EOF, retry from EOF.

3. Trailing-slash on `?PAT/` also needed the EOF retry chain.
"""
from __future__ import annotations

import os
from pathlib import Path

import supertool


# ---------------------------------------------------------------------------
# Forward `/PAT` BOF-retry hardening
# ---------------------------------------------------------------------------

def test_forward_search_miss_mid_cursor_retries_from_bof(tmp_path: Path) -> None:
    """Forward search miss + persisted cursor mid-file → retry from BOF and log note."""
    persist_dir = tmp_path / "persist"
    persist_dir.mkdir()
    os.environ.pop("SUPERTOOL_VIM_NO_PERSIST", None)
    os.environ["SUPERTOOL_VIM_PERSIST_DIR"] = str(persist_dir)
    try:
        f = tmp_path / "x.txt"
        # 60 lines: 'target' is on line 5, cursor will land at line 50.
        lines = [f"line{n}" for n in range(1, 61)]
        lines[4] = "target"  # line 5 (index 4)
        f.write_text("\n".join(lines) + "\n")

        # Persist cursor at line 50.
        out1 = supertool.op_vim(str(f), "50G")
        assert "ERROR" not in out1, out1

        # Search forward for 'target' — would miss from line 50, BOF retry finds it.
        out2 = supertool.op_vim(str(f), "/target")
        assert "ERROR" not in out2, out2
        assert "retried from BOF" in out2, out2
    finally:
        os.environ.pop("SUPERTOOL_VIM_PERSIST_DIR", None)


def test_forward_search_at_bof_no_retry_needed(tmp_path: Path, monkeypatch) -> None:
    """Forward search miss with cursor=0 → no retry, error as today."""
    monkeypatch.setenv("SUPERTOOL_VIM_NO_PERSIST", "1")
    f = tmp_path / "x.txt"
    f.write_text("foo\nbar\n")
    out = supertool.op_vim(str(f), "/notthere")
    assert "ERROR" in out
    assert "not found" in out
    assert "retried from BOF" not in out


def test_forward_search_truly_absent_errors_after_retry(tmp_path: Path) -> None:
    """Pattern truly absent — error even after BOF retry attempt."""
    persist_dir = tmp_path / "persist"
    persist_dir.mkdir()
    os.environ.pop("SUPERTOOL_VIM_NO_PERSIST", None)
    os.environ["SUPERTOOL_VIM_PERSIST_DIR"] = str(persist_dir)
    try:
        f = tmp_path / "x.txt"
        f.write_text("foo\nbar\nbaz\nquux\nzz\n")
        out1 = supertool.op_vim(str(f), "3G")
        assert "ERROR" not in out1
        out2 = supertool.op_vim(str(f), "/nowhere")
        assert "ERROR" in out2
        assert "not found" in out2
    finally:
        os.environ.pop("SUPERTOOL_VIM_PERSIST_DIR", None)


def test_forward_search_trailing_slash_chains_to_bof_retry(tmp_path: Path) -> None:
    """`/foo/` with trimmed `foo` still missing from cursor → BOF retry on trimmed."""
    persist_dir = tmp_path / "persist"
    persist_dir.mkdir()
    os.environ.pop("SUPERTOOL_VIM_NO_PERSIST", None)
    os.environ["SUPERTOOL_VIM_PERSIST_DIR"] = str(persist_dir)
    try:
        f = tmp_path / "x.txt"
        lines = [f"line{n}" for n in range(1, 31)]
        lines[2] = "target"  # line 3
        f.write_text("\n".join(lines) + "\n")
        # Persist cursor at line 20.
        out1 = supertool.op_vim(str(f), "20G")
        assert "ERROR" not in out1
        # `/target/` — trailing-slash autocorrect strips to `target`,
        # which is at line 3 (before cursor) → must also try BOF.
        out2 = supertool.op_vim(str(f), "/target/")
        assert "ERROR" not in out2, out2
        assert "retried from BOF" in out2, out2
    finally:
        os.environ.pop("SUPERTOOL_VIM_PERSIST_DIR", None)


# ---------------------------------------------------------------------------
# Backward `?PAT` EOF-retry (new)
# ---------------------------------------------------------------------------

def test_backward_search_miss_from_bof_retries_from_eof(tmp_path: Path, monkeypatch) -> None:
    """`?PAT` from cursor=0 with match later in file → retry from EOF."""
    monkeypatch.setenv("SUPERTOOL_VIM_NO_PERSIST", "1")
    f = tmp_path / "x.txt"
    f.write_text("foo\nbar\ntarget\nquux\n")
    # Fresh call → cursor=0. `?target` would normally miss (cursor at top,
    # nothing earlier). EOF retry finds 'target' on line 3.
    out = supertool.op_vim(str(f), "?target␞iX")
    assert "ERROR" not in out, out
    assert f.read_text(encoding="utf-8") == "foo\nbar\nXtarget\nquux\n"


def test_backward_search_miss_logs_eof_retry_note(tmp_path: Path, monkeypatch) -> None:
    """Backward EOF retry should leave a note in the receipt for transparency."""
    monkeypatch.setenv("SUPERTOOL_VIM_NO_PERSIST", "1")
    f = tmp_path / "x.txt"
    f.write_text("a\nb\ntarget\nc\n")
    out = supertool.op_vim(str(f), "?target")
    assert "ERROR" not in out, out
    assert "retried from EOF" in out, out


def test_backward_search_at_eof_no_retry(tmp_path: Path) -> None:
    """`?PAT` with cursor already at EOF — no retry needed; standard miss errors."""
    persist_dir = tmp_path / "persist"
    persist_dir.mkdir()
    os.environ.pop("SUPERTOOL_VIM_NO_PERSIST", None)
    os.environ["SUPERTOOL_VIM_PERSIST_DIR"] = str(persist_dir)
    try:
        f = tmp_path / "x.txt"
        f.write_text("foo\nbar\nbaz\n")
        # Persist cursor at end (G goes to last line BOL).
        out1 = supertool.op_vim(str(f), "G$")
        assert "ERROR" not in out1
        out2 = supertool.op_vim(str(f), "?notthere")
        assert "ERROR" in out2
        assert "not found" in out2
        assert "retried from EOF" not in out2
    finally:
        os.environ.pop("SUPERTOOL_VIM_PERSIST_DIR", None)


def test_backward_search_truly_absent_errors_after_retry(tmp_path: Path, monkeypatch) -> None:
    """Backward search miss + EOF retry also missing → error."""
    monkeypatch.setenv("SUPERTOOL_VIM_NO_PERSIST", "1")
    f = tmp_path / "x.txt"
    f.write_text("foo\nbar\n")
    out = supertool.op_vim(str(f), "?nowhere")
    assert "ERROR" in out
    assert "not found" in out


def test_backward_search_trailing_slash_chains_to_eof_retry(tmp_path: Path, monkeypatch) -> None:
    """`?target/` from cursor=0 → trim slash + EOF retry."""
    monkeypatch.setenv("SUPERTOOL_VIM_NO_PERSIST", "1")
    f = tmp_path / "x.txt"
    f.write_text("a\nb\ntarget\nc\n")
    out = supertool.op_vim(str(f), "?target/")
    assert "ERROR" not in out, out
    assert "retried from EOF" in out, out


# ---------------------------------------------------------------------------
# Combined: persist cursor via one action, search in next, autocorrect fires
# ---------------------------------------------------------------------------

def test_combined_persist_then_search_autocorrect(tmp_path: Path) -> None:
    """End-to-end: previous call left cursor mid-file, next call searches forward
    for an earlier pattern → BOF retry fires and edit lands correctly."""
    persist_dir = tmp_path / "persist"
    persist_dir.mkdir()
    os.environ.pop("SUPERTOOL_VIM_NO_PERSIST", None)
    os.environ["SUPERTOOL_VIM_PERSIST_DIR"] = str(persist_dir)
    try:
        f = tmp_path / "x.txt"
        f.write_text("alpha\nbeta\ngamma\ndelta\nepsilon\n")
        # Call 1: persist cursor at line 4 (delta).
        out1 = supertool.op_vim(str(f), "4G")
        assert "ERROR" not in out1
        # Call 2: search for 'alpha' (line 1) and insert 'X' before it.
        out2 = supertool.op_vim(str(f), "/alpha␞iX")
        assert "ERROR" not in out2, out2
        assert "retried from BOF" in out2
        assert f.read_text(encoding="utf-8") == "Xalpha\nbeta\ngamma\ndelta\nepsilon\n"
    finally:
        os.environ.pop("SUPERTOOL_VIM_PERSIST_DIR", None)
