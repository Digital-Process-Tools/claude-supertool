"""Tests for op_vi — vi-flavored cursor-based multi-action edit op."""
from __future__ import annotations

from pathlib import Path

import supertool


# ---------------------------------------------------------------------------
# Cursor movement
# ---------------------------------------------------------------------------

def test_gg_jumps_to_bof(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("a\nb\nc\n")
    out = supertool.op_vi(str(f), "G;gg;ifirst ")
    assert f.read_text() == "first a\nb\nc\n"


def test_G_jumps_to_eof(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("a\nb\nc\n")
    out = supertool.op_vi(str(f), "G;iEND")
    assert f.read_text() == "a\nb\nc\nEND"


def test_nG_goto_line(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("a\nb\nc\nd\n")
    out = supertool.op_vi(str(f), "3G;i*")
    assert f.read_text() == "a\nb\n*c\nd\n"


def test_dollar_lands_on_last_char(tmp_path: Path) -> None:
    """vi: `$` lands on last char of line. `i` then inserts BEFORE it."""
    f = tmp_path / "x.py"
    f.write_text("hello\n")
    out = supertool.op_vi(str(f), "$;i!")
    # cursor on 'o', insert '!' before → "hell!o\n"
    assert f.read_text() == "hell!o\n"


def test_A_appends_at_eol(tmp_path: Path) -> None:
    """For 'insert at end of line', use A — appends before the trailing \\n."""
    f = tmp_path / "x.py"
    f.write_text("hello\n")
    out = supertool.op_vi(str(f), "A!")
    assert f.read_text() == "hello!\n"


def test_zero_to_bol(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("hello\n")
    out = supertool.op_vi(str(f), "$;0;i>")
    assert f.read_text() == ">hello\n"


def test_search_forward(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("foo bar baz\n")
    out = supertool.op_vi(str(f), "/bar;i!")
    assert f.read_text() == "foo !bar baz\n"


def test_search_backward(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("foo bar foo bar\n")
    out = supertool.op_vi(str(f), "G;?foo;i!")
    assert f.read_text() == "foo bar !foo bar\n"


def test_search_forward_not_found(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("foo\n")
    out = supertool.op_vi(str(f), "/missing;i!")
    assert "ERROR" in out and "not found" in out
    assert f.read_text() == "foo\n"


def test_l_count_move_right(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("abcdef\n")
    out = supertool.op_vi(str(f), "3l;i!")
    assert f.read_text() == "abc!def\n"


def test_h_clamps_to_zero(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("abc\n")
    out = supertool.op_vi(str(f), "$;99h;i!")
    assert f.read_text() == "!abc\n"


def test_j_moves_down(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("aa\nbb\ncc\n")
    out = supertool.op_vi(str(f), "2j;i!")
    assert f.read_text() == "aa\nbb\n!cc\n"


# ---------------------------------------------------------------------------
# Inserts
# ---------------------------------------------------------------------------

def test_insert_before_cursor(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("world\n")
    out = supertool.op_vi(str(f), "ihello ")
    assert f.read_text() == "hello world\n"


def test_append_after_cursor(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("ab\n")
    out = supertool.op_vi(str(f), "ax")
    assert f.read_text() == "axb\n"


def test_insert_at_bol(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("middle\n")
    out = supertool.op_vi(str(f), "$;I> ")
    assert f.read_text() == "> middle\n"


def test_append_at_eol(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("middle\n")
    out = supertool.op_vi(str(f), "0;A <")
    assert f.read_text() == "middle <\n"


def test_open_below(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("title\n")
    out = supertool.op_vi(str(f), "/title;obody")
    assert f.read_text() == "title\nbody\n"


def test_open_above(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("body\n")
    out = supertool.op_vi(str(f), "/body;Ohead")
    assert f.read_text() == "head\nbody\n"


def test_open_chain_block_before_marker(tmp_path: Path) -> None:
    """Classic Kevin use case: insert a multi-line block before a heading."""
    f = tmp_path / "x.md"
    f.write_text("## Process\n")
    out = supertool.op_vi(
        str(f), "/## Process;O## Task list;o1. Foo;o2. Bar"
    )
    assert f.read_text() == "## Task list\n1. Foo\n2. Bar\n## Process\n"


def test_insert_with_newline_escape(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("ab\n")
    out = supertool.op_vi(str(f), "ix\\ny\\nz")
    assert f.read_text() == "x\ny\nzab\n"


def test_insert_with_count_repeats(tmp_path: Path) -> None:
    """vi: 5i- inserts '-' 5 times."""
    f = tmp_path / "x.py"
    f.write_text("ok\n")
    out = supertool.op_vi(str(f), "5i-")
    assert f.read_text() == "-----ok\n"


# ---------------------------------------------------------------------------
# Deletes
# ---------------------------------------------------------------------------

def test_x_delete_one_char(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("abc\n")
    out = supertool.op_vi(str(f), "x")
    assert f.read_text() == "bc\n"


def test_nx_delete_n_chars(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("abcdef\n")
    out = supertool.op_vi(str(f), "3x")
    assert f.read_text() == "def\n"


def test_dd_delete_one_line(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("a\nb\nc\n")
    out = supertool.op_vi(str(f), "2G;dd")
    assert f.read_text() == "a\nc\n"


def test_ndd_delete_n_lines(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("a\nb\nc\nd\ne\n")
    out = supertool.op_vi(str(f), "2G;3dd")
    assert f.read_text() == "a\ne\n"


def test_D_delete_to_eol(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("keep|drop\nnext\n")
    out = supertool.op_vi(str(f), "/|;D")
    assert f.read_text() == "keep\nnext\n"


# ---------------------------------------------------------------------------
# Replace
# ---------------------------------------------------------------------------

def test_replace_first_char(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("hello\n")
    out = supertool.op_vi(str(f), "rH")
    assert f.read_text() == "Hello\n"


def test_replace_after_search(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("x = 1\n")
    out = supertool.op_vi(str(f), "/1;r9")
    assert f.read_text() == "x = 9\n"


def test_replace_at_eof_errors(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("a")
    out = supertool.op_vi(str(f), "G;rX")
    assert "ERROR" in out


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

def test_missing_file(tmp_path: Path) -> None:
    out = supertool.op_vi(str(tmp_path / "nope.py"), "iabc")
    assert "ERROR: file not found" in out


def test_empty_script(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("a\n")
    out = supertool.op_vi(str(f), "")
    assert "ERROR: empty script" in out


def test_unknown_verb(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("a\n")
    out = supertool.op_vi(str(f), "Zbogus")
    assert "ERROR" in out and "unknown verb" in out
    assert f.read_text() == "a\n"


def test_goto_beyond_eof(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("a\n")
    out = supertool.op_vi(str(f), "99G;ix")
    assert "ERROR" in out and "out of range" in out


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def test_dispatch_triple_colon(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("foo\n")
    out = supertool.dispatch(f"vi:::{f}:::A end")
    assert "vi " in out
    assert f.read_text() == "foo end\n"


def test_dispatch_missing_script(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("foo\n")
    out = supertool.dispatch(f"vi:::{f}:::")
    assert "ERROR: empty script" in out


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------

def test_receipt_shows_cursor_and_context(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("a\nb\nc\nd\ne\n")
    out = supertool.op_vi(str(f), "3G;iX")
    assert "cursor at 3:" in out
    assert "--- context ---" in out
    assert "→" in out


# ---------------------------------------------------------------------------
# UTF-8 / special chars
# ---------------------------------------------------------------------------

def test_insert_emoji(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("hello\n")
    out = supertool.op_vi(str(f), "$;a 🦫")
    assert f.read_text() == "hello 🦫\n"


def test_insert_regex_meta_chars(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("x\n")
    out = supertool.op_vi(str(f), "i.*+?[]()$^")
    assert f.read_text() == ".*+?[]()$^x\n"
