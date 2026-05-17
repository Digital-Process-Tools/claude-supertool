"""Tests for the `:!` ex shell-filter verb.

Vim's `:!` runs a shell command. Three flavors are supported:

- `:!cmd`     — run cmd, insert stdout after cursor line (no range)
- `:%!cmd`    — pipe whole buffer through cmd, replace buffer with stdout
- `:N,M!cmd`  — pipe lines N..M through cmd, replace those lines

WARNING: cmd runs with the same OS privileges as supertool itself.
"""
from __future__ import annotations

from pathlib import Path

import supertool


def test_bang_bare_inserts_stdout_after_cursor(tmp_path: Path) -> None:
    """`:!echo hi` inserts the command's stdout after the cursor's line."""
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nc\n")
    # cursor on line 1 (default), :!echo hi appends "hi" after line 1
    out = supertool.op_vim(str(f), ":!echo hi")
    assert "ERROR" not in out, out
    assert f.read_text() == "a\nhi\nb\nc\n"


def test_bang_percent_pipes_whole_buffer(tmp_path: Path) -> None:
    """`:%!tr a-z A-Z` upcases the whole buffer."""
    f = tmp_path / "x.txt"
    f.write_text("abc\ndef\n")
    out = supertool.op_vim(str(f), ":%!tr a-z A-Z")
    assert "ERROR" not in out, out
    assert f.read_text() == "ABC\nDEF\n"


def test_bang_range_pipes_selected_lines(tmp_path: Path) -> None:
    """`:2,3!tr a-z A-Z` only upcases lines 2..3."""
    f = tmp_path / "x.txt"
    f.write_text("aaa\nbbb\nccc\nddd\n")
    out = supertool.op_vim(str(f), ":2,3!tr a-z A-Z")
    assert "ERROR" not in out, out
    assert f.read_text() == "aaa\nBBB\nCCC\nddd\n"
