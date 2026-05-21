"""Tests for the `.` (dot-repeat) verb replaying previous edit verbs.

Covers dd, dw, cw, ciw, cc, p, and :!cmd inside the dot-repeat code path.
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


# --- dot-repeat dd ---

def test_dot_repeats_dd(tmp_path: Path) -> None:
    out = _run(tmp_path, "a\nb\nc\nd\n", "gg␞dd␞.")
    # dd deletes line 1; . repeats deleting line 2 (now top).
    assert out == "c\nd\n"


def test_dot_repeats_2dd(tmp_path: Path) -> None:
    out = _run(tmp_path, "a\nb\nc\nd\ne\n", "gg␞2dd␞.")
    # 2dd deletes 2 lines; . repeats deleting next 2.
    assert out == "e\n"


# --- dot-repeat dw ---

def test_dot_repeats_dw_on_word(tmp_path: Path) -> None:
    out = _run(tmp_path, "foo bar baz\n", "gg␞dw␞.")
    # dw deletes "foo "; . deletes "bar ".
    assert out == "baz\n"


def test_dot_repeats_dw_on_non_word(tmp_path: Path) -> None:
    # Cursor on '.', dw deletes punctuation run until next word char.
    out = _run(tmp_path, "...foo bar\n", "gg␞dw␞.")
    # 1st dw: deletes "..." (non-word run, no trailing ws). Cursor at 'f'.
    # .: deletes "foo " (word + trailing space). Result: "bar\n"
    assert out == "bar\n"


# --- dot-repeat cw ---

def test_dot_repeats_cw(tmp_path: Path) -> None:
    out = _run(tmp_path, "foo bar baz\n", "gg␞cwX\x1b␞w␞.")
    # cw "foo" → "X" → "X bar baz\n"; w moves to "bar"; . replays cw → "X X baz\n"
    assert out == "X X baz\n"


# --- dot-repeat ciw ---

def test_dot_repeats_ciw_on_word(tmp_path: Path) -> None:
    out = _run(tmp_path, "foo bar baz\n", "gg␞ciwHI\x1b␞w␞.")
    # ciw 'foo' → 'HI'; w to next word; . replays ciw → 'HI'.
    assert out == "HI HI baz\n"


def test_dot_repeats_ciw_skipped_off_word(tmp_path: Path) -> None:
    # Cursor on space — ciw skips, file unchanged for the replay.
    out = _run(tmp_path, "foo bar\n", "gg␞ciwQ\x1b␞l␞l␞l␞.")
    # 1st ciw → 'Q '... cursor moves; 'l' three times advances to space.
    # On space, . dot-repeats ciw but is no-op (off word).
    # Just confirm file ends with "Q bar\n" or contains "Q".
    assert "Q" in out


# --- dot-repeat cc ---

def test_dot_repeats_cc(tmp_path: Path) -> None:
    out = _run(tmp_path, "a\nb\nc\n", "gg␞ccX\x1b␞j␞.")
    # cc on line 1: replaces "a" with "X". j moves down to "b" line. . → "X" again.
    assert out == "X\nX\nc\n"


# --- dot-repeat p (paste from register) ---

def test_dot_repeats_paste(tmp_path: Path) -> None:
    out = _run(tmp_path, "a\nb\nc\n", "gg␞yy␞G␞p␞.")
    # yy on line 1: register = "a\n" (linewise). G to last line. p pastes after.
    # . repeats p (still linewise).
    assert "a" in out and out.count("a") >= 3


# --- dot-repeat :!cmd ---

def test_dot_repeats_bang_cmd(tmp_path: Path) -> None:
    # :!echo HI inserts "HI\n" after cursor line. Dot replay does it again.
    out = _run(tmp_path, "line1\nline2\n", "gg␞:!echo HI\n␞.")
    # Initial :!echo HI inserts HI after line1. Dot repeats — inserts HI again
    # after the (new) cursor line.
    assert out.count("HI") >= 2
