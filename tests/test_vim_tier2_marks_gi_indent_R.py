"""Tier 2 vim: indent ops (>><<, >motion, <motion), R overwrite, marks (m '`), gi."""
from __future__ import annotations

from pathlib import Path

import supertool


# ---------------------------------------------------------------------------
# Indent: >> << (current line)
# ---------------------------------------------------------------------------

def test_shift_right_indents_current_line(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("hello\nworld\n")
    supertool.op_vim(str(f), "gg␞>>")
    assert f.read_text(encoding="utf-8") == "    hello\nworld\n"


def test_shift_left_dedents_current_line(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("    hello\nworld\n")
    supertool.op_vim(str(f), "gg␞<<")
    assert f.read_text(encoding="utf-8") == "hello\nworld\n"


def test_shift_left_dedents_partial_spaces(tmp_path: Path) -> None:
    """<< removes up to 4 leading spaces (or fewer if line has less)."""
    f = tmp_path / "x.txt"
    f.write_text("  hi\n")
    supertool.op_vim(str(f), "gg␞<<")
    assert f.read_text(encoding="utf-8") == "hi\n"


def test_shift_left_dedents_tab(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("\thi\n")
    supertool.op_vim(str(f), "gg␞<<")
    assert f.read_text(encoding="utf-8") == "hi\n"


def test_count_shift_right(tmp_path: Path) -> None:
    """5>> indents 5 lines from cursor."""
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nc\nd\ne\nf\n")
    supertool.op_vim(str(f), "gg␞5>>")
    assert f.read_text(encoding="utf-8") == "    a\n    b\n    c\n    d\n    e\nf\n"


# ---------------------------------------------------------------------------
# Indent with motion: >iw, >{, >}, <ap
# ---------------------------------------------------------------------------

def test_shift_right_iw_indents_word_line(tmp_path: Path) -> None:
    """>iw indents the line(s) covered by the text-object."""
    f = tmp_path / "x.txt"
    f.write_text("hello world\nsecond\n")
    supertool.op_vim(str(f), "gg␞>iw")
    assert f.read_text(encoding="utf-8") == "    hello world\nsecond\n"


def test_shift_right_paragraph(tmp_path: Path) -> None:
    """>} indents lines from cursor to next paragraph boundary."""
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nc\n\nd\n")
    supertool.op_vim(str(f), "gg␞>}")
    # Indents lines 1-3 (covered by motion to paragraph boundary)
    assert f.read_text(encoding="utf-8") == "    a\n    b\n    c\n\nd\n"


def test_shift_left_around_paragraph(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("    a\n    b\n    c\n")
    supertool.op_vim(str(f), "gg␞<ap")
    assert f.read_text(encoding="utf-8") == "a\nb\nc\n"


# ---------------------------------------------------------------------------
# R — overwrite mode
# ---------------------------------------------------------------------------

def test_R_overwrites_chars(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("hello world\n")
    supertool.op_vim(str(f), "gg␞Rabc")
    assert f.read_text(encoding="utf-8") == "abclo world\n"


def test_R_past_eol_appends(tmp_path: Path) -> None:
    """R going past end of line appends remaining chars."""
    f = tmp_path / "x.txt"
    f.write_text("abc\nnext\n")
    # Cursor at offset 1 (b), R "XYZW" overwrites bc then appends W
    supertool.op_vim(str(f), "gg␞l␞RXYZW")
    assert f.read_text(encoding="utf-8") == "aXYZW\nnext\n"


def test_R_stops_at_newline(tmp_path: Path) -> None:
    """R does not overwrite the newline — it appends past EOL within the same line."""
    f = tmp_path / "x.txt"
    f.write_text("ab\ncd\n")
    supertool.op_vim(str(f), "gg␞Rxyz")
    # R at offset 0 overwrites a, b then 'z' appends within line 1, not crossing \n
    assert f.read_text(encoding="utf-8") == "xyz\ncd\n"


# ---------------------------------------------------------------------------
# Marks: m{a-z}, '{m}, `{m}
# ---------------------------------------------------------------------------

def test_mark_set_and_jump_line(tmp_path: Path) -> None:
    """ma sets mark, 'a jumps to that line (first non-blank)."""
    f = tmp_path / "x.txt"
    f.write_text("line1\n  line2\nline3\n")
    # Go to offset within line2, set mark a, move away, jump back with 'a then insert X
    supertool.op_vim(str(f), "gg␞j␞l␞ma␞gg␞'a␞iX")
    # 'a goes to first non-blank of line2 (offset 8 = first 'l' after "  ")
    assert f.read_text(encoding="utf-8") == "line1\n  Xline2\nline3\n"


def test_mark_set_and_jump_exact(tmp_path: Path) -> None:
    """`a jumps to exact byte offset of the mark."""
    f = tmp_path / "x.txt"
    f.write_text("hello world\n")
    # Set mark at offset 6 (w), move to BOF, jump back with `a, insert X
    supertool.op_vim(str(f), "gg␞6l␞ma␞gg␞`a␞iX")
    assert f.read_text(encoding="utf-8") == "hello Xworld\n"


def test_mark_survives_edit(tmp_path: Path) -> None:
    """Mark persists across edits in same session."""
    f = tmp_path / "x.txt"
    f.write_text("hello world\n")
    supertool.op_vim(str(f), "gg␞6l␞mz␞gg␞iA␞␞`z␞iY")
    # After "iA" at BOF: "Ahello world\n", mark z was at offset 6 (still points to 6)
    # `z jumps to offset 6 → which is now char " " (space). iY inserts Y before it.
    assert f.read_text(encoding="utf-8") == "Ahello Yworld\n"


def test_mark_uppercase_treated_as_lowercase(tmp_path: Path) -> None:
    """mA (global) treated same as ma for our single-file scope."""
    f = tmp_path / "x.txt"
    f.write_text("hello world\n")
    supertool.op_vim(str(f), "gg␞6l␞mA␞gg␞`A␞iX")
    assert f.read_text(encoding="utf-8") == "hello Xworld\n"


def test_marks_persist_across_op_vim_calls(tmp_path: Path, monkeypatch) -> None:
    """Marks survive separate op_vim() invocations via persist file."""
    monkeypatch.delenv("SUPERTOOL_VIM_NO_PERSIST", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    f = tmp_path / "x.txt"
    f.write_text("hello world\n")
    supertool.op_vim(str(f), "gg␞6l␞ma")
    supertool.op_vim(str(f), "gg␞`a␞iZ")
    assert f.read_text(encoding="utf-8") == "hello Zworld\n"


# ---------------------------------------------------------------------------
# gi — insert at last edit position
# ---------------------------------------------------------------------------

def test_gi_returns_to_last_edit(tmp_path: Path) -> None:
    """After insert, gi jumps back to that position and resumes insert mode."""
    f = tmp_path / "x.txt"
    f.write_text("hello world\n")
    # First insert at offset 6, then move away, then gi to resume and insert "World"
    supertool.op_vim(str(f), "gg␞6l␞iHello␞gg␞giWorld")
    # First insert produces: "hello Helloworld\n" with cursor at 11 (after "Hello")
    # gi jumps back to that pos (11) and inserts "World" → "hello HelloWorldworld\n"
    assert f.read_text(encoding="utf-8") == "hello HelloWorldworld\n"


def test_gi_persists_across_calls(tmp_path: Path, monkeypatch) -> None:
    """gi works across separate op_vim() calls."""
    monkeypatch.delenv("SUPERTOOL_VIM_NO_PERSIST", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    f = tmp_path / "x.txt"
    f.write_text("hello\n")
    supertool.op_vim(str(f), "gg␞2l␞iX")
    # Now cursor is past 'X'. Move to BOF in another call, then gi.
    supertool.op_vim(str(f), "gg␞giY")
    # After first call: "heXllo\n", last_edit was at offset 3 (after X).
    # Second call: gi → offset 3, insert Y → "heXYllo\n"
    assert f.read_text(encoding="utf-8") == "heXYllo\n"
