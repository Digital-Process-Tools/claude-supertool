"""Tests for op_vim insert-mode-entry family: s, S, C.

These are vim shortcuts that combine a delete + enter insert mode in one verb:
  s  — substitute char(s):  Ns deletes N chars from cursor, enters insert.
  S  — substitute line(s):  NS deletes N whole lines (cursor's line down),
       enters insert at start of that line.
  C  — change to EOL:       same as c$ — delete cursor→EOL, enter insert.

gi (insert at last edit position) is intentionally NOT implemented — would
require cross-call last-insert-position tracking. Skipped per task.
"""
from __future__ import annotations

from pathlib import Path

import supertool


# ---------------------------------------------------------------------------
# s — substitute char(s) at cursor
# ---------------------------------------------------------------------------

def test_s_deletes_char_under_cursor_and_inserts_text(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("hello\n")
    # cursor at BOF (on 'h'); s deletes 'h', inserts "HEY"
    supertool.op_vim(str(f), "gg␞sHEY␞")
    assert f.read_text() == "HEYello\n"


def test_s_bare_just_deletes_char(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("hello\n")
    # bare s with no TEXT just deletes the char (like x)
    supertool.op_vim(str(f), "gg␞s␞")
    assert f.read_text() == "ello\n"


def test_3s_deletes_three_chars_and_inserts(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("hello world\n")
    # cursor at BOF; 3s deletes "hel", inserts "HI"
    supertool.op_vim(str(f), "gg␞3sHI␞")
    assert f.read_text() == "HIlo world\n"


def test_s_mid_line(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("abcdef\n")
    # cursor at 'c' (offset 2); s deletes 'c', inserts "X"
    supertool.op_vim(str(f), "gg␞2l␞sX␞")
    assert f.read_text() == "abXdef\n"


# ---------------------------------------------------------------------------
# S — substitute whole line(s)
# ---------------------------------------------------------------------------

def test_S_replaces_current_line(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("aaa\nbbb\nccc\n")
    # go to line 2, S replaces line content (keeps trailing \n)
    supertool.op_vim(str(f), "2G␞SNEW␞")
    assert f.read_text() == "aaa\nNEW\nccc\n"


def test_S_bare_just_blanks_line(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("aaa\nbbb\nccc\n")
    supertool.op_vim(str(f), "2G␞S␞")
    assert f.read_text() == "aaa\n\nccc\n"


def test_2S_replaces_two_lines(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nc\nd\n")
    # at line 2, 2S replaces lines 2+3 with single TEXT
    supertool.op_vim(str(f), "2G␞2SX␞")
    assert f.read_text() == "a\nX\nd\n"


def test_S_starts_insert_at_BOL(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("    indented\nnext\n")
    # cursor mid-line should not matter — S goes whole-line, insert at BOL
    supertool.op_vim(str(f), "gg␞5l␞Sreplaced␞")
    assert f.read_text() == "replaced\nnext\n"


# ---------------------------------------------------------------------------
# C — change cursor to EOL
# ---------------------------------------------------------------------------

def test_C_deletes_to_eol_and_inserts(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("hello world\n")
    # cursor at offset 6 ('w'); C deletes "world", inserts "moon"
    supertool.op_vim(str(f), "gg␞6l␞Cmoon␞")
    assert f.read_text() == "hello moon\n"


def test_C_bare_just_deletes_to_eol(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("hello world\n")
    supertool.op_vim(str(f), "gg␞6l␞C␞")
    assert f.read_text() == "hello \n"


def test_C_at_BOL_replaces_whole_line(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("aaa\nbbb\n")
    # C at BOL of line 1 should delete "aaa", insert "X" → "X\nbbb\n"
    supertool.op_vim(str(f), "gg␞CX␞")
    assert f.read_text() == "X\nbbb\n"


def test_C_preserves_trailing_newline(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("abc\ndef\n")
    # C should NOT eat the \n — only deletes to EOL, not past it
    supertool.op_vim(str(f), "gg␞CZZ␞")
    assert f.read_text() == "ZZ\ndef\n"

# ---------------------------------------------------------------------------
# o / O auto-indent — per vim default behavior
# ---------------------------------------------------------------------------

def test_o_auto_indents_to_current_line(tmp_path: Path) -> None:
    """`o` should open a new line below cursor AND auto-indent to match the
    current line's leading whitespace. Per real vim default behavior.
    """
    f = tmp_path / "x.py"
    f.write_text("def foo():\n    if True:\n        pass\n")
    # Search for `if True:` (8 spaces? no, 4 spaces), then `o` to open below
    # and insert content. Expected: new line at 4-space indent (matching `if`).
    supertool.op_vim(str(f), "/if True:\x1bonew_line = 1\x1b")
    assert f.read_text() == "def foo():\n    if True:\n    new_line = 1\n        pass\n"


def test_O_auto_indents_to_current_line(tmp_path: Path) -> None:
    """`O` should open a new line above cursor AND auto-indent to match the
    current line's leading whitespace.
    """
    f = tmp_path / "x.py"
    f.write_text("def foo():\n    if True:\n        pass\n")
    supertool.op_vim(str(f), "/if True:\x1bOnew_line = 1\x1b")
    assert f.read_text() == "def foo():\n    new_line = 1\n    if True:\n        pass\n"


def test_o_explicit_indent_in_text_appends_to_auto_indent(tmp_path: Path) -> None:
    """When the user provides explicit leading whitespace in the o-insert text,
    it appends to the auto-indent (does not replace it). This mirrors vim's
    default — autoindent stays, your typing adds on top.
    """
    f = tmp_path / "x.py"
    f.write_text("    foo\n")
    # Insert "extra" with NO explicit leading whitespace → auto-indent only (4 sp)
    supertool.op_vim(str(f), "ggoextra\x1b")
    assert f.read_text() == "    foo\n    extra\n"


def test_o_zero_indent_line(tmp_path: Path) -> None:
    """`o` on a line with no leading whitespace should produce no auto-indent."""
    f = tmp_path / "x.py"
    f.write_text("nothing\n")
    supertool.op_vim(str(f), "ggoadded\x1b")
    assert f.read_text() == "nothing\nadded\n"
