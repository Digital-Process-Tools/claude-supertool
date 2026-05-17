"""Tier 1 vim grab-bag: Y, gJ, *, #, :sort, :reverse, :move, :copy, :retab, :r !, :norm."""
from __future__ import annotations

from pathlib import Path

import supertool


# ---------------------------------------------------------------------------
# Y — yank to EOL (alias for y$, NOT yy)
# ---------------------------------------------------------------------------

def test_Y_yanks_to_eol_then_paste(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("hello world\nsecond\n")
    # Cursor at start of "world" (col 7), Y yanks "world", paste after EOL
    supertool.op_vim(str(f), "gg␞fw␞Y␞$␞p")
    assert f.read_text() == "hello worldworld\nsecond\n"


def test_Y_is_not_yy(tmp_path: Path) -> None:
    """Y must NOT be linewise like yy — paste after cursor is char-wise."""
    f = tmp_path / "x.txt"
    f.write_text("abcdef\nsecond\n")
    # Move cursor to position 2 (char 'c'), Y yanks "cdef" (char-wise),
    # then move to BOL of line 2 and paste — should land char-wise after 's'
    supertool.op_vim(str(f), "gg␞2l␞Y␞j␞0␞p")
    # line2 starts 'second' → after position 0 paste of "cdef" → "scdefecond"
    assert f.read_text() == "abcdef\nscdefecond\n"


# ---------------------------------------------------------------------------
# gJ — join lines without space (vs J with space)
# ---------------------------------------------------------------------------

def test_gJ_joins_without_space(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("foo\nbar\n")
    supertool.op_vim(str(f), "gg␞gJ")
    assert f.read_text() == "foobar\n"


def test_J_vs_gJ(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("hello\n  world\n")
    supertool.op_vim(str(f), "gg␞J")
    # J: replaces \n + leading whitespace with single space
    assert f.read_text() == "hello world\n"

    f2 = tmp_path / "y.txt"
    f2.write_text("hello\n  world\n")
    supertool.op_vim(str(f2), "gg␞gJ")
    # gJ: just removes the \n, preserves leading whitespace as-is
    assert f2.read_text() == "hello  world\n"


# ---------------------------------------------------------------------------
# * — search forward for word under cursor
# ---------------------------------------------------------------------------

def test_star_jumps_to_next_word(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("foo bar foo baz\n")
    # Cursor on first "foo", * jumps to second "foo"
    supertool.op_vim(str(f), "gg␞*␞iX")
    assert f.read_text() == "foo bar Xfoo baz\n"


def test_star_word_boundary(tmp_path: Path) -> None:
    """* must match whole words only — 'foo' should not match 'foobar'."""
    f = tmp_path / "x.txt"
    f.write_text("foo foobar foo end\n")
    supertool.op_vim(str(f), "gg␞*␞iX")
    # Should skip 'foobar' and land on second standalone 'foo'
    assert f.read_text() == "foo foobar Xfoo end\n"


# ---------------------------------------------------------------------------
# # — search backward for word under cursor
# ---------------------------------------------------------------------------

def test_hash_jumps_backward(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("foo bar foo baz\n")
    # Move to second 'foo' (offset 8), then # back to first
    supertool.op_vim(str(f), "gg␞/foo␞n␞#␞iX")
    # n moved to second foo (col 8), # back to first foo (col 0)
    assert f.read_text() == "Xfoo bar foo baz\n"


def test_hash_word_boundary(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("foo foobar foo end\n")
    # * from first 'foo' jumps to second standalone 'foo' (skips 'foobar');
    # # back to first 'foo'.
    supertool.op_vim(str(f), "gg␞*␞#␞iY")
    assert f.read_text() == "Yfoo foobar foo end\n"


# ---------------------------------------------------------------------------
# :sort — line sort
# ---------------------------------------------------------------------------

def test_sort_ascending(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("banana\napple\ncherry\n")
    supertool.op_vim(str(f), ":sort")
    assert f.read_text() == "apple\nbanana\ncherry\n"


def test_sort_descending(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("banana\napple\ncherry\n")
    supertool.op_vim(str(f), ":sort!")
    assert f.read_text() == "cherry\nbanana\napple\n"


def test_sort_unique(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("b\na\nb\nc\na\n")
    supertool.op_vim(str(f), ":sort u")
    assert f.read_text() == "a\nb\nc\n"


def test_sort_numeric(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("10\n2\n100\n21\n")
    supertool.op_vim(str(f), ":sort n")
    assert f.read_text() == "2\n10\n21\n100\n"


def test_sort_range(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("HEADER\nc\na\nb\nFOOTER\n")
    supertool.op_vim(str(f), ":2,4sort")
    assert f.read_text() == "HEADER\na\nb\nc\nFOOTER\n"


# ---------------------------------------------------------------------------
# :reverse — reverse line order
# ---------------------------------------------------------------------------

def test_reverse_whole_file(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nc\n")
    supertool.op_vim(str(f), ":reverse")
    assert f.read_text() == "c\nb\na\n"


def test_reverse_range(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("HEADER\na\nb\nc\nFOOTER\n")
    supertool.op_vim(str(f), ":2,4reverse")
    assert f.read_text() == "HEADER\nc\nb\na\nFOOTER\n"


# ---------------------------------------------------------------------------
# :move — move lines
# ---------------------------------------------------------------------------

def test_move_range(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n")
    # Move lines 3-5 to after line 10. Result: 1,2,6,7,8,9,10,3,4,5
    supertool.op_vim(str(f), ":3,5m 10")
    assert f.read_text() == "1\n2\n6\n7\n8\n9\n10\n3\n4\n5\n"


def test_move_single_line(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nc\nd\n")
    # Move line 1 to after line 3 → b, c, a, d
    supertool.op_vim(str(f), ":1move 3")
    assert f.read_text() == "b\nc\na\nd\n"


# ---------------------------------------------------------------------------
# :copy / :t — copy lines
# ---------------------------------------------------------------------------

def test_copy_to_top(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nc\n")
    # :3copy 0 — copy line 3 to after line 0 (top)
    supertool.op_vim(str(f), ":3copy 0")
    assert f.read_text() == "c\na\nb\nc\n"


def test_t_is_alias_for_copy(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nc\n")
    supertool.op_vim(str(f), ":3t 0")
    assert f.read_text() == "c\na\nb\nc\n"


# ---------------------------------------------------------------------------
# :retab — tabs → spaces
# ---------------------------------------------------------------------------

def test_retab_converts_tabs(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("\thello\n\t\tworld\n")
    supertool.op_vim(str(f), ":retab 4")
    assert f.read_text() == "    hello\n        world\n"


def test_retab_default_width(tmp_path: Path) -> None:
    """Default width 4 (no arg)."""
    f = tmp_path / "x.txt"
    f.write_text("\tfoo\n")
    supertool.op_vim(str(f), ":retab")
    assert f.read_text() == "    foo\n"


# ---------------------------------------------------------------------------
# :r !CMD — insert shell output
# ---------------------------------------------------------------------------

def test_r_bang_inserts_shell_output(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("first\nlast\n")
    supertool.op_vim(str(f), "gg␞:r !echo hello")
    # :r inserts after current line → first, hello, last
    assert f.read_text() == "first\nhello\nlast\n"


def test_r_bang_multiline_output(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("top\nend\n")
    supertool.op_vim(str(f), "gg␞:r !printf 'a\\nb\\nc'")
    assert f.read_text() == "top\na\nb\nc\nend\n"


# ---------------------------------------------------------------------------
# :norm — run normal-mode cmds per line in range
# ---------------------------------------------------------------------------

def test_norm_append_bang_to_every_line(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nc\n")
    # %norm A!\e — append "!" to every line
    supertool.op_vim(str(f), ":%norm A!")
    assert f.read_text() == "a!\nb!\nc!\n"


def test_norm_range(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nc\nd\n")
    # Only lines 2,3 get the trailing X
    supertool.op_vim(str(f), ":2,3norm AX")
    assert f.read_text() == "a\nbX\ncX\nd\n"
