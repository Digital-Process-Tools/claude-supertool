"""Tests for op_vim number ops (\\C-a / \\C-x) and case verbs (~ g~ gu gU).

Escape syntax for Ctrl-a / Ctrl-x in scripts:
  `\\C-a` → `\\x01` (real Ctrl-A) — increment number under/after cursor
  `\\C-x` → `\\x18` (real Ctrl-X) — decrement

Case verbs:
  `~`            — toggle case of char at cursor, advance (N~ toggles N chars)
  `g~<motion>`   — toggle case over motion (g~$ to EOL, g~w word, etc.)
  `gu<motion>`   — lowercase over motion
  `gU<motion>`   — uppercase over motion
  `g~~`/`guu`/`gUU` — operate on current line
"""
from __future__ import annotations

from pathlib import Path

import supertool


# ---------------------------------------------------------------------------
# ~ — toggle case of single char + advance
# ---------------------------------------------------------------------------

def test_tilde_toggles_single_char_and_advances(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("hello\n")
    # cursor at BOF (on 'h'); ~ toggles 'h' → 'H'
    supertool.op_vim(str(f), "gg␞~␞")
    assert f.read_text(encoding="utf-8") == "Hello\n"


def test_tilde_count_toggles_n_chars(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("hello\n")
    # cursor at BOF; 3~ toggles "hel" → "HEL"
    supertool.op_vim(str(f), "gg␞3~␞")
    assert f.read_text(encoding="utf-8") == "HELlo\n"


def test_tilde_toggles_uppercase_to_lower(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("HELLO\n")
    supertool.op_vim(str(f), "gg␞5~␞")
    assert f.read_text(encoding="utf-8") == "hello\n"


def test_tilde_skips_non_letters(tmp_path: Path) -> None:
    """Non-letters pass through unchanged but cursor still advances."""
    f = tmp_path / "x.txt"
    f.write_text("a1b2c\n")
    supertool.op_vim(str(f), "gg␞5~␞")
    assert f.read_text(encoding="utf-8") == "A1B2C\n"


# ---------------------------------------------------------------------------
# g~<motion> — toggle case over motion
# ---------------------------------------------------------------------------

def test_g_tilde_dollar_toggles_to_eol(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("hello world\n")
    supertool.op_vim(str(f), "gg␞g~$␞")
    assert f.read_text(encoding="utf-8") == "HELLO WORLD\n"


def test_g_tilde_tilde_toggles_current_line(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("Hello World\nLine Two\n")
    supertool.op_vim(str(f), "gg␞g~~␞")
    assert f.read_text(encoding="utf-8") == "hELLO wORLD\nLine Two\n"


# ---------------------------------------------------------------------------
# gu<motion> — lowercase over motion
# ---------------------------------------------------------------------------

def test_gu_dollar_lowercases_to_eol(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("HELLO WORLD\n")
    supertool.op_vim(str(f), "gg␞gu$␞")
    assert f.read_text(encoding="utf-8") == "hello world\n"


def test_guu_lowercases_current_line(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("HELLO WORLD\nNEXT\n")
    supertool.op_vim(str(f), "gg␞guu␞")
    assert f.read_text(encoding="utf-8") == "hello world\nNEXT\n"


# ---------------------------------------------------------------------------
# gU<motion> — uppercase over motion
# ---------------------------------------------------------------------------

def test_gU_dollar_uppercases_to_eol(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("hello world\n")
    supertool.op_vim(str(f), "gg␞gU$␞")
    assert f.read_text(encoding="utf-8") == "HELLO WORLD\n"


def test_gUU_uppercases_current_line(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("hello world\nnext\n")
    supertool.op_vim(str(f), "gg␞gUU␞")
    assert f.read_text(encoding="utf-8") == "HELLO WORLD\nnext\n"


# ---------------------------------------------------------------------------
# \C-a / \C-x — increment / decrement number
# ---------------------------------------------------------------------------

def test_ctrl_a_increments_number(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("count = 42\n")
    # cursor at BOF; \C-a finds first digit run and increments
    supertool.op_vim(str(f), "gg␞\\C-a␞")
    assert f.read_text(encoding="utf-8") == "count = 43\n"


def test_ctrl_a_with_count_increments_by_n(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("count = 42\n")
    supertool.op_vim(str(f), "gg␞5\\C-a␞")
    assert f.read_text(encoding="utf-8") == "count = 47\n"


def test_ctrl_x_decrements_number(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("count = 42\n")
    supertool.op_vim(str(f), "gg␞\\C-x␞")
    assert f.read_text(encoding="utf-8") == "count = 41\n"


def test_ctrl_x_can_produce_negative(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("n = 0\n")
    supertool.op_vim(str(f), "gg␞\\C-x␞")
    assert f.read_text(encoding="utf-8") == "n = -1\n"


def test_ctrl_a_on_digit_under_cursor(tmp_path: Path) -> None:
    """Cursor sitting on a digit: increments that number, not a later one."""
    f = tmp_path / "x.txt"
    f.write_text("10 and 20\n")
    # cursor at BOF (on '1'); \C-a increments the 10 → 11
    supertool.op_vim(str(f), "gg␞\\C-a␞")
    assert f.read_text(encoding="utf-8") == "11 and 20\n"
