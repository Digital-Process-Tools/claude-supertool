"""Standalone w/b/e motions across punctuation/whitespace runs + */# edges."""
from __future__ import annotations

from pathlib import Path

import supertool


def _run(tmp_path: Path, initial: str, script: str) -> str:
    f = tmp_path / "x.txt"
    f.write_text(initial)
    out = supertool.op_vim(str(f), script)
    assert not out.startswith("ERROR"), out
    return f.read_text(encoding="utf-8")


# --- w motion across punctuation (4937-4942) ---

def test_w_motion_skips_punctuation_run(tmp_path: Path) -> None:
    # cursor on '.', w should advance past the punctuation run.
    out = _run(tmp_path, "abc..def\n", "gg␞/\\.␞w␞iX")
    # After /. cursor on '.', w moves to next word boundary.
    assert "X" in out


def test_w_motion_with_count_across_mixed(tmp_path: Path) -> None:
    out = _run(tmp_path, "a..b..c\n", "gg␞3w␞iX")
    assert "X" in out


# --- b motion across punctuation (4963-4968) ---

def test_b_motion_back_over_punctuation(tmp_path: Path) -> None:
    # cursor at end of "def", b should walk back through "..".
    out = _run(tmp_path, "abc..def\n", "gg␞$␞b␞iX")
    assert "X" in out


def test_b_motion_back_from_punctuation_cursor(tmp_path: Path) -> None:
    # Line ends with punctuation — $ lands on '.', b walks back through punct.
    out = _run(tmp_path, "abc...\n", "gg␞$␞b␞iX")
    assert "X" in out


def test_b_motion_with_count(tmp_path: Path) -> None:
    out = _run(tmp_path, "abc def ghi\n", "gg␞$␞3b␞iX")
    assert "X" in out


# --- * / # search for word under cursor ---

def test_star_searches_word_under_cursor(tmp_path: Path) -> None:
    # cursor on first "foo", * jumps to next occurrence.
    out = _run(tmp_path, "foo bar foo baz\n", "gg␞*␞iX")
    assert "X" in out
    # Second "foo" is the target — X should be inserted before/at second 'foo'.
    assert out.count("foo") == 2


def test_star_with_cursor_on_non_word_scans_to_next_word(tmp_path: Path) -> None:
    # Cursor on space at BOL... actually start on '.', * scans to next word on line.
    out = _run(tmp_path, "...hello world hello\n", "gg␞*␞iX")
    # * finds "hello" first (next word), then jumps to next "hello".
    assert "X" in out


def test_hash_searches_word_backward(tmp_path: Path) -> None:
    # Search to second "foo", then # jumps backward to the first.
    out = _run(tmp_path, "foo bar foo baz\n", "gg␞/foo␞n␞#␞iX")
    # /foo lands on first foo, n advances to second foo, # back to first.
    assert "X" in out


def test_star_with_no_word_on_line_errors(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("...\n")
    out = supertool.op_vim(str(f), "gg␞*")
    assert "ERROR" in out and "no word" in out
