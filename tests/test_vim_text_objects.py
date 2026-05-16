"""Tests for op_vim full text-object family.

Text objects combine with operators d/c/y/g~/gu/gU:
  iw aw    — word (inner / around)
  iW aW    — WORD (whitespace-separated)
  is as    — sentence
  ip ap    — paragraph
  i" a"    — double-quoted string
  i' a'    — single-quoted string
  i` a`    — backtick-quoted string
  i( a(    — parens (ib/ab aliases)
  i[ a[    — brackets
  i{ a{    — braces (iB/aB aliases)
  i< a<    — angle brackets
  it at    — HTML/XML tags
"""
from __future__ import annotations

from pathlib import Path

import supertool


# ---------------------------------------------------------------------------
# word: iw / aw
# ---------------------------------------------------------------------------

def test_diw_deletes_word_under_cursor(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("foo bar baz\n")
    # gg → cursor BOF on 'f'; w moves to 'b' of bar; diw deletes 'bar'
    supertool.op_vim(str(f), "gg␞w␞diw␞")
    assert f.read_text() == "foo  baz\n"


def test_daw_deletes_word_and_trailing_space(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("foo bar baz\n")
    supertool.op_vim(str(f), "gg␞w␞daw␞")
    assert f.read_text() == "foo baz\n"


def test_ciw_change_word(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("foo bar baz\n")
    supertool.op_vim(str(f), "gg␞w␞ciwQUX␞")
    assert f.read_text() == "foo QUX baz\n"


def test_yiw_then_paste(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("foo bar\n")
    # yank 'foo', move to end, paste after — 'foo bar'+'foo' inserted after cursor
    supertool.op_vim(str(f), "gg␞yiw␞$␞p␞")
    assert "foo" in f.read_text()
    assert f.read_text().count("foo") == 2


def test_gUiw_uppercases_word(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("foo bar baz\n")
    supertool.op_vim(str(f), "gg␞w␞gUiw␞")
    assert f.read_text() == "foo BAR baz\n"


def test_guiw_lowercases_word(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("foo BAR baz\n")
    supertool.op_vim(str(f), "gg␞w␞guiw␞")
    assert f.read_text() == "foo bar baz\n"


def test_g_tilde_iw_toggles_word(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("foo BaR baz\n")
    supertool.op_vim(str(f), "gg␞w␞g~iw␞")
    assert f.read_text() == "foo bAr baz\n"


# ---------------------------------------------------------------------------
# WORD: iW / aW (whitespace-separated)
# ---------------------------------------------------------------------------

def test_diW_deletes_punctuated_word(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("foo a-b-c baz\n")
    # cursor on 'a' of a-b-c
    supertool.op_vim(str(f), "gg␞w␞diW␞")
    assert f.read_text() == "foo  baz\n"


def test_daW_deletes_punctuated_word_with_space(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("foo a-b-c baz\n")
    supertool.op_vim(str(f), "gg␞w␞daW␞")
    assert f.read_text() == "foo baz\n"


# ---------------------------------------------------------------------------
# sentence: is / as
# ---------------------------------------------------------------------------

def test_dis_deletes_sentence(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("First sentence. Second one. Third here.\n")
    # cursor at BOF — deletes "First sentence."
    supertool.op_vim(str(f), "gg␞dis␞")
    txt = f.read_text()
    assert "First sentence" not in txt
    assert "Second one" in txt


def test_das_deletes_sentence_with_trailing_space(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("First sentence. Second one. Third here.\n")
    supertool.op_vim(str(f), "gg␞das␞")
    txt = f.read_text()
    assert txt.startswith("Second")


# ---------------------------------------------------------------------------
# paragraph: ip / ap
# ---------------------------------------------------------------------------

def test_dip_deletes_paragraph(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("para one\nstill one\n\npara two\nmore\n")
    supertool.op_vim(str(f), "gg␞dip␞")
    txt = f.read_text()
    assert "para one" not in txt
    assert "para two" in txt


def test_dap_deletes_paragraph_with_blank_lines(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("para one\nstill one\n\npara two\nmore\n")
    supertool.op_vim(str(f), "gg␞dap␞")
    txt = f.read_text()
    assert "para one" not in txt
    assert txt.startswith("para two")


def test_guip_lowercases_paragraph(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("HELLO WORLD\nMORE TEXT\n\nOTHER PARA\n")
    supertool.op_vim(str(f), "gg␞guip␞")
    txt = f.read_text()
    assert "hello world" in txt
    assert "more text" in txt
    assert "OTHER PARA" in txt  # second paragraph untouched


# ---------------------------------------------------------------------------
# quoted: i" / a" / i' / a' / i`
# ---------------------------------------------------------------------------

def test_di_dquote_deletes_inner_string(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text('name = "hello world"\n')
    supertool.op_vim(str(f), 'gg␞di"␞')
    assert f.read_text() == 'name = ""\n'


def test_da_dquote_deletes_string_with_quotes(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text('name = "hello world"\n')
    supertool.op_vim(str(f), 'gg␞da"␞')
    assert f.read_text() == "name = \n"


def test_di_squote_deletes_inner_single_quoted(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("name = 'hello'\n")
    supertool.op_vim(str(f), "gg␞di'␞")
    assert f.read_text() == "name = ''\n"


def test_da_squote_deletes_single_quoted_with_quotes(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("name = 'hello'\n")
    supertool.op_vim(str(f), "gg␞da'␞")
    assert f.read_text() == "name = \n"


def test_di_backtick_deletes_inner(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("name = `code`\n")
    supertool.op_vim(str(f), "gg␞di`␞")
    assert f.read_text() == "name = ``\n"


def test_yi_dquote_yanks_inner_content(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text('a = "hi"\nb = ""\n')
    # yank "hi", go to line 2's empty quotes, paste inside
    supertool.op_vim(str(f), 'gg␞yi"␞j␞f"␞p␞')
    txt = f.read_text()
    assert 'b = "hi"' in txt


# ---------------------------------------------------------------------------
# parens / brackets / braces / angle: i( a( i[ a[ i{ a{ i< a<
# ---------------------------------------------------------------------------

def test_di_paren_deletes_inner(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("call(foo, bar)\n")
    supertool.op_vim(str(f), "gg␞di(␞")
    assert f.read_text() == "call()\n"


def test_da_paren_deletes_with_parens(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("call(foo, bar)\n")
    supertool.op_vim(str(f), "gg␞da(␞")
    assert f.read_text() == "call\n"


def test_di_close_paren_same_as_open(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("call(foo, bar)\n")
    supertool.op_vim(str(f), "gg␞di)␞")
    assert f.read_text() == "call()\n"


def test_dib_alias_for_paren(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("call(foo, bar)\n")
    supertool.op_vim(str(f), "gg␞dib␞")
    assert f.read_text() == "call()\n"


def test_di_bracket_deletes_inner(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("arr[index]\n")
    supertool.op_vim(str(f), "gg␞di[␞")
    assert f.read_text() == "arr[]\n"


def test_di_bracket_close_same(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("arr[index]\n")
    supertool.op_vim(str(f), "gg␞di]␞")
    assert f.read_text() == "arr[]\n"


def test_da_bracket_with_delims(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("arr[index]\n")
    supertool.op_vim(str(f), "gg␞da[␞")
    assert f.read_text() == "arr\n"


def test_di_brace_deletes_inner(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("obj{key: val}\n")
    supertool.op_vim(str(f), "gg␞di{␞")
    assert f.read_text() == "obj{}\n"


def test_di_brace_close_same(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("obj{key: val}\n")
    supertool.op_vim(str(f), "gg␞di}␞")
    assert f.read_text() == "obj{}\n"


def test_diB_alias_for_brace(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("obj{key: val}\n")
    supertool.op_vim(str(f), "gg␞diB␞")
    assert f.read_text() == "obj{}\n"


def test_di_angle_deletes_inner(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("tag<gen, ric>\n")
    supertool.op_vim(str(f), "gg␞di<␞")
    assert f.read_text() == "tag<>\n"


def test_da_angle_with_delims(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("tag<gen, ric>\n")
    supertool.op_vim(str(f), "gg␞da<␞")
    assert f.read_text() == "tag\n"


def test_ci_paren_changes_inner(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("call(foo, bar)\n")
    supertool.op_vim(str(f), "gg␞ci(QUX␞")
    assert f.read_text() == "call(QUX)\n"


# ---------------------------------------------------------------------------
# HTML tags: it / at
# ---------------------------------------------------------------------------

def test_dit_deletes_tag_inner(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("<span>hi</span>\n")
    supertool.op_vim(str(f), "gg␞dit␞")
    assert f.read_text() == "<span></span>\n"


def test_dat_deletes_full_tag(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("before<span>hi</span>after\n")
    supertool.op_vim(str(f), "gg␞f<␞dat␞")
    assert f.read_text() == "beforeafter\n"


def test_cit_changes_tag_inner(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("<b>old</b>\n")
    supertool.op_vim(str(f), "gg␞citnew␞")
    assert f.read_text() == "<b>new</b>\n"
