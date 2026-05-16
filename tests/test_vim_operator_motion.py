"""Tests for op_vim operator-motion family: d/y + G, gg, /PAT, ?PAT, ^, h, j, k, l.

c<motion> family is intentionally skipped — the parser currently can't cleanly
split the search-arg terminator (\\e) from the trailing insert TEXT for forms
like `c/PAT\\eTEXT\\e`. Limited to delete + yank, which require no insert text.
"""
from __future__ import annotations

from pathlib import Path

import supertool


# ---------------------------------------------------------------------------
# dG — delete from cursor's line to EOF (inclusive)
# ---------------------------------------------------------------------------

def test_dG_from_bof_deletes_whole_file(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nc\nd\ne\n")
    supertool.op_vim(str(f), "gg␞dG")
    assert f.read_text() == ""


def test_dG_from_line3_of_5_leaves_first_2(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nc\nd\ne\n")
    supertool.op_vim(str(f), "gg␞3G␞dG")
    assert f.read_text() == "a\nb\n"


# ---------------------------------------------------------------------------
# dgg — delete from cursor's line up to BOF (line 1) inclusive
# ---------------------------------------------------------------------------

def test_dgg_from_line3_of_5_deletes_first_3(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nc\nd\ne\n")
    supertool.op_vim(str(f), "gg␞3G␞dgg")
    assert f.read_text() == "d\ne\n"


# ---------------------------------------------------------------------------
# d/PAT — delete from cursor up to start of next PAT match
# ---------------------------------------------------------------------------

def test_d_search_forward_deletes_through_match_start(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("hello world end\n")
    # cursor at BOF; d/world␞ should delete "hello " (up to start of "world")
    supertool.op_vim(str(f), "gg␞d/world␞")
    assert f.read_text() == "world end\n"


# ---------------------------------------------------------------------------
# d?PAT — delete from cursor back to last PAT match
# ---------------------------------------------------------------------------

def test_d_search_backward_deletes_to_match(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("alpha beta gamma\n")
    # land on 'g' in "gamma" via /gamma, then d?beta␞ should delete back to
    # the start of "beta", removing "beta " — leaving "alpha gamma\n".
    supertool.op_vim(str(f), "gg␞/gamma␞d?beta ␞")
    assert f.read_text() == "alpha gamma\n"


# ---------------------------------------------------------------------------
# d^ — delete to first non-blank of line
# ---------------------------------------------------------------------------

def test_d_caret_deletes_to_first_nonblank(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("    hello world\n")
    # Move to the 'w' in world, then d^ deletes back to first non-blank ('h').
    supertool.op_vim(str(f), "gg␞/world␞d^")
    assert f.read_text() == "    world\n"


# ---------------------------------------------------------------------------
# dh dl — delete one char left / right
# ---------------------------------------------------------------------------

def test_dl_deletes_char_at_cursor(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("abcdef\n")
    # cursor on 'a', dl deletes 'a'
    supertool.op_vim(str(f), "gg␞dl")
    assert f.read_text() == "bcdef\n"


def test_dh_deletes_char_before_cursor(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("abcdef\n")
    # land on 'd' (index 3), dh deletes char before cursor ('c')
    supertool.op_vim(str(f), "gg␞3l␞dh")
    assert f.read_text() == "abdef\n"


# ---------------------------------------------------------------------------
# dj dk — delete current line + line below / above
# ---------------------------------------------------------------------------

def test_dj_deletes_current_and_next_line(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nc\nd\n")
    # cursor on line 2 ('b'), dj deletes 'b' and 'c'
    supertool.op_vim(str(f), "gg␞2G␞dj")
    assert f.read_text() == "a\nd\n"


def test_dk_deletes_current_and_previous_line(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nc\nd\n")
    # cursor on line 3 ('c'), dk deletes 'b' and 'c'
    supertool.op_vim(str(f), "gg␞3G␞dk")
    assert f.read_text() == "a\nd\n"


# ---------------------------------------------------------------------------
# yG — yank from cursor's line to EOF, then p pastes
# ---------------------------------------------------------------------------

def test_yG_then_p_pastes_lines_to_eof(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nc\nd\ne\n")
    # yank lines 3..5 ("c\nd\ne\n"), go to line 1, p pastes after line 1.
    supertool.op_vim(str(f), "gg␞3G␞yG␞gg␞p")
    assert f.read_text() == "a\nc\nd\ne\nb\nc\nd\ne\n"


# ---------------------------------------------------------------------------
# ygg — yank from cursor's line back to BOF
# ---------------------------------------------------------------------------

def test_ygg_then_p_pastes_lines_to_bof(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nc\nd\ne\n")
    # On line 3, ygg yanks lines 1..3 ("a\nb\nc\n"). Move to EOF, p pastes after.
    supertool.op_vim(str(f), "gg␞3G␞ygg␞G␞p")
    assert f.read_text() == "a\nb\nc\nd\ne\na\nb\nc\n"


# ---------------------------------------------------------------------------
# y/PAT — yank from cursor up to start of next PAT match
# ---------------------------------------------------------------------------

def test_y_search_forward_then_P_pastes_before_cursor(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("hello world end\n")
    # yank "hello " (cursor at BOF up to "world"), then P at cursor pastes
    # before — duplicating "hello " at BOF.
    supertool.op_vim(str(f), "gg␞y/world␞P")
    assert f.read_text() == "hello hello world end\n"


# ---------------------------------------------------------------------------
# y^ y h y l y j y k — basic charwise/linewise yank smoke
# ---------------------------------------------------------------------------

def test_yl_then_p_duplicates_char(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("abc\n")
    # cursor on 'a', yl yanks 'a', p pastes after cursor → "aabc\n"
    supertool.op_vim(str(f), "gg␞yl␞p")
    assert f.read_text() == "aabc\n"


def test_yh_then_p_yanks_prev_char(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("abc\n")
    # cursor on 'b' (idx 1), yh yanks 'a' (char before cursor)
    supertool.op_vim(str(f), "gg␞l␞yh␞$␞p")
    # cursor lands on 'c' via $; p inserts after 'c' → "abcca"... wait, register='a'
    # After yh, cursor stays; $ moves to last char 'c'; p inserts after 'c' → "abca\n"
    assert f.read_text() == "abca\n"
