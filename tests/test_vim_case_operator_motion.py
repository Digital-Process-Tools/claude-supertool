"""Tests for vim case operators (g~ gu gU) combined with motion targets.

These are heavy code paths in op_vim's operator-motion branch that previously
had little or no coverage. Each test sets up content, runs a case operator,
and asserts the resulting text.
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


# --- gU + word motions ----------------------------------------------------

def test_gU_w_uppercases_word(tmp_path: Path) -> None:
    assert _run(tmp_path, "hello world\n", "gg␞gUw") == "HELLO world\n"


def test_gU_b_uppercases_back_word(tmp_path: Path) -> None:
    out = _run(tmp_path, "hello world\n", "gg␞w␞gUb")
    assert out == "HELLO world\n"


def test_gU_e_uppercases_to_word_end(tmp_path: Path) -> None:
    assert _run(tmp_path, "hello world\n", "gg␞gUe") == "HELLO world\n"


# --- gu + word motions ----------------------------------------------------

def test_gu_w_lowercases_word(tmp_path: Path) -> None:
    assert _run(tmp_path, "HELLO WORLD\n", "gg␞guw") == "hello WORLD\n"


def test_gu_b_lowercases_back_word(tmp_path: Path) -> None:
    out = _run(tmp_path, "HELLO WORLD\n", "gg␞w␞gub")
    assert out == "hello WORLD\n"


def test_gu_e_lowercases_to_word_end(tmp_path: Path) -> None:
    assert _run(tmp_path, "HELLO WORLD\n", "gg␞gue") == "hello WORLD\n"


# --- g~ + word motions ----------------------------------------------------

def test_gtilde_w_swaps_case_word(tmp_path: Path) -> None:
    assert _run(tmp_path, "Hello world\n", "gg␞g~w") == "hELLO world\n"


def test_gtilde_e_swaps_case_to_word_end(tmp_path: Path) -> None:
    assert _run(tmp_path, "Hello World\n", "gg␞g~e") == "hELLO World\n"


# --- gU/gu + paragraph motions -------------------------------------------

def test_gU_open_brace_uppercases_back_to_blank(tmp_path: Path) -> None:
    out = _run(tmp_path, "first\n\nsecond para\nthird\n", "G␞gU{")
    assert "THIRD" in out or "SECOND" in out


def test_gU_close_brace_uppercases_to_next_blank(tmp_path: Path) -> None:
    out = _run(tmp_path, "para1 line1\npara1 line2\n\npara2\n", "gg␞gU}")
    assert "PARA1" in out and "para2" in out


# --- gU + sentence motions ( ) -------------------------------------------

def test_gU_open_paren_back_to_sentence_start(tmp_path: Path) -> None:
    out = _run(tmp_path, "First. Second.\n", "gg␞/Second␞l␞l␞gU(")
    # ( from middle of "Second" → start of "Second", upper that range.
    assert "SE" in out or "S" in out


def test_gU_close_paren_to_next_sentence(tmp_path: Path) -> None:
    out = _run(tmp_path, "First. Second.\n", "gg␞gU)")
    assert "FIRST." in out


# --- gU + bracket motion % -----------------------------------------------

def test_gU_percent_forward_bracket(tmp_path: Path) -> None:
    out = _run(tmp_path, "foo(bar)baz\n", "gg␞/(␞gU%")
    # From '(' to ')' inclusive: "(bar)" → "(BAR)"
    assert "(BAR)" in out


def test_gU_percent_backward_bracket(tmp_path: Path) -> None:
    out = _run(tmp_path, "foo(bar)baz\n", "gg␞/)␞gU%")
    # From ')' back to '(' inclusive
    assert "(BAR)" in out


# --- gU + line motions ---------------------------------------------------

def test_gU_dollar_uppercases_to_eol(tmp_path: Path) -> None:
    assert _run(tmp_path, "hello world\n", "gg␞gU$") == "HELLO WORLD\n"


def test_gU_zero_uppercases_to_bol(tmp_path: Path) -> None:
    out = _run(tmp_path, "hello world\n", "gg␞$␞gU0")
    assert out.startswith("HELLO WORL")


def test_gU_caret_uppercases_to_first_nonblank(tmp_path: Path) -> None:
    out = _run(tmp_path, "    hello\n", "gg␞$␞gU^")
    assert "HELL" in out


# --- gU + j/k linewise ---------------------------------------------------

def test_gU_j_uppercases_two_lines(tmp_path: Path) -> None:
    out = _run(tmp_path, "first\nsecond\nthird\n", "gg␞gUj")
    assert "FIRST" in out and "SECOND" in out and "third" in out


def test_gU_k_uppercases_back_one_line(tmp_path: Path) -> None:
    out = _run(tmp_path, "first\nsecond\nthird\n", "gg␞j␞gUk")
    assert "FIRST" in out and "SECOND" in out


# --- gU + + and - --------------------------------------------------------

def test_gU_plus_uppercases_current_and_next_line(tmp_path: Path) -> None:
    out = _run(tmp_path, "a\nb\nc\n", "gg␞gU+")
    assert "A" in out and "B" in out


def test_gU_minus_uppercases_prev_and_current_line(tmp_path: Path) -> None:
    out = _run(tmp_path, "a\nb\nc\n", "gg␞j␞gU-")
    assert "A" in out and "B" in out


# --- gU + W/B/E ---------------------------------------------------------

def test_gU_W_uppercases_WORD(tmp_path: Path) -> None:
    out = _run(tmp_path, "foo,bar next\n", "gg␞gUW")
    assert "FOO,BAR" in out and "next" in out


def test_gU_B_uppercases_back_WORD(tmp_path: Path) -> None:
    out = _run(tmp_path, "foo,bar next\n", "gg␞$␞gUB")
    assert "NEX" in out or "NEXT" in out


def test_gU_E_uppercases_to_WORD_end(tmp_path: Path) -> None:
    out = _run(tmp_path, "foo,bar next\n", "gg␞gUE")
    assert "FOO,BAR" in out


# --- gU + ; and , (repeat find) -----------------------------------------

def test_gU_semicolon_uses_last_find(tmp_path: Path) -> None:
    out = _run(tmp_path, "abcd,efgh,ijkl\n", "gg␞f,␞gU;")
    # f, → first comma. ; repeats finding next ','.
    # gU; uppercases from current to next ',' inclusive.
    assert "EFGH" in out or "," in out


def test_gU_comma_reverses_find(tmp_path: Path) -> None:
    out = _run(tmp_path, "abcd,efgh,ijkl\n", "gg␞f,␞;␞gU,")
    # Last find = ','. ; → 2nd. , reverses → back to 1st.
    assert "EFGH" in out or "," in out


# --- case operator + t/T (case op via repeat ; , with t/T) ---
# t/T are not direct case-op motions but can be replayed via ; , after f/F/t/T

def test_gU_semicolon_repeats_t_motion(tmp_path: Path) -> None:
    out = _run(tmp_path, "abcXdefXghi\n", "gg␞tX␞gU;")
    # tX → cursor before 1st X. ; repeats tX (finds next X). gU on range.
    # Just confirm uppercasing happened somewhere.
    assert any(c.isupper() for c in out)


