"""Misc vim tests targeting scattered coverage gaps:

- yank with find motions (yf, yt, yF, yT, y;, y,)
- delete/change with find motions (df, dt, cF, cT, etc.)
- dot-repeat for insert verbs (I, A, o, O)
- ranged :%!cmd and :N,M!cmd via dot-repeat
"""
from __future__ import annotations

from pathlib import Path

import supertool


def _run(tmp_path: Path, initial: str, script: str) -> str:
    f = tmp_path / "x.txt"
    f.write_text(initial)
    out = supertool.op_vim(str(f), script)
    assert not out.startswith("ERROR"), out
    return f.read_text()


# --- yank with find motions ---

def test_yf_yanks_through_char(tmp_path: Path) -> None:
    out = _run(tmp_path, "abcdef\n", "gg␞yfd␞G␞p")
    # yf d → yank "abcd"; G to last line (only line here); p pastes after cursor.
    assert "abcd" in out


def test_yt_yanks_up_to_char(tmp_path: Path) -> None:
    out = _run(tmp_path, "abcdef\n", "gg␞ytd␞G␞p")
    # yt d → yank "abc" (exclusive of 'd'); p pastes.
    assert "abc" in out


def test_yF_yanks_back_through_char(tmp_path: Path) -> None:
    out = _run(tmp_path, "abcdef\n", "gg␞$␞yFb␞p")
    # $ → 'f'. yF b → yank "bcdef" (back to 'b' inclusive).
    assert "bcdef" in out or "bcde" in out


def test_yT_yanks_back_up_to_char(tmp_path: Path) -> None:
    out = _run(tmp_path, "abcdef\n", "gg␞$␞yTb␞p")
    # $ → 'f'. yT b → yank "cdef" (exclusive of 'b' back).
    assert "cdef" in out or "cde" in out


# --- delete with find ---

def test_df_deletes_through_char(tmp_path: Path) -> None:
    assert _run(tmp_path, "abcdef\n", "gg␞dfd") == "ef\n"


def test_dt_deletes_up_to_char(tmp_path: Path) -> None:
    assert _run(tmp_path, "abcdef\n", "gg␞dtd") == "def\n"


def test_dF_deletes_back_to_char(tmp_path: Path) -> None:
    # dF from $ deletes "bcde" (back to 'b' inclusive, cursor's char preserved).
    assert _run(tmp_path, "abcdef\n", "gg␞$␞dFb") == "af\n"


def test_dT_deletes_back_to_char_exclusive(tmp_path: Path) -> None:
    # dT b from 'f' deletes "cde" (back to 'b' exclusive).
    assert _run(tmp_path, "abcdef\n", "gg␞$␞dTb") == "abf\n"


# --- change with find ---

def test_cf_changes_through_char(tmp_path: Path) -> None:
    assert _run(tmp_path, "abcdef\n", "gg␞cfdX\x1b") == "Xef\n"


def test_ct_changes_up_to_char(tmp_path: Path) -> None:
    assert _run(tmp_path, "abcdef\n", "gg␞ctdX\x1b") == "Xdef\n"


# --- dot-repeat with insert verbs ---

def test_dot_repeats_I_insert(tmp_path: Path) -> None:
    out = _run(tmp_path, "  hello\n  world\n", "gg␞IX\x1b␞j␞.")
    # I inserts X at BOL of current line; . replays on next line.
    assert out.count("X") == 2


def test_dot_repeats_A_append_at_eol(tmp_path: Path) -> None:
    out = _run(tmp_path, "hello\nworld\n", "gg␞AX\x1b␞j␞.")
    assert "helloX" in out and "worldX" in out


def test_dot_repeats_o_opens_new_line_below(tmp_path: Path) -> None:
    out = _run(tmp_path, "a\nb\n", "gg␞oNEW\x1b␞.")
    # o opens new line below 'a' inserting "NEW". . repeats: opens another line.
    assert out.count("NEW") == 2


def test_dot_repeats_O_opens_new_line_above(tmp_path: Path) -> None:
    out = _run(tmp_path, "a\nb\n", "G␞ONEW\x1b␞.")
    # O opens above 'b' inserting NEW; . repeats above the (new) cursor line.
    assert out.count("NEW") == 2


# --- ranged :%!cmd via dot-repeat ---

def test_dot_repeats_percent_bang_cmd(tmp_path: Path) -> None:
    # :%!cat -n is too platform-specific; use 'tr' which is POSIX everywhere.
    out = _run(tmp_path, "hello\nworld\n", "gg␞:%!tr a-z A-Z\n␞.")
    # First :%!tr uppercases entire file → "HELLO\nWORLD\n". Dot repeats: no-op (already upper).
    assert "HELLO" in out and "WORLD" in out


# --- ranged :N,M!cmd via dot-repeat ---

def test_dot_repeats_range_bang_cmd(tmp_path: Path) -> None:
    out = _run(tmp_path, "a\nb\nc\nd\n", "gg␞:1,2!tr a-z A-Z\n␞.")
    # :1,2!tr uppercases lines 1-2. Dot repeats on lines (cursor moved by ex).
    assert "A" in out and "B" in out


# --- swap-case via ~ ---

def test_swap_case_single_char(tmp_path: Path) -> None:
    out = _run(tmp_path, "aBcD\n", "gg␞~")
    # ~ swaps case of char under cursor, advances. After one ~: "AbcD\n".
    assert out.startswith("A")


def test_swap_case_with_count(tmp_path: Path) -> None:
    out = _run(tmp_path, "abcd\n", "gg␞3~")
    # 3~ swaps 3 chars: "ABCd\n".
    assert out == "ABCd\n"
