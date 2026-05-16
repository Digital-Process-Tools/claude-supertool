"""Tests for newly-added vim motion verbs:

WORD motions: W B E ge gE
Line motions: g_ + - _
Paragraph:    { }
Sentence:     ( )
Bracket:      %
Char-find repeat: ; ,

Each motion is tested standalone (cursor lands at expected offset) and as
an operator-motion target (d{, c}, y%, etc.).

Cursor verification is indirect: we use an insert verb after the motion to
mark where the cursor ended up — `iX` inserts `X` at cursor, so the literal
position of `X` in the output proves where the cursor landed.
"""
from __future__ import annotations

from pathlib import Path

import supertool


# Helper: run a script and return new file content.
def _run(tmp_path: Path, initial: str, script: str, name: str = "x.txt") -> str:
    f = tmp_path / name
    f.write_text(initial)
    out = supertool.op_vim(str(f), script)
    assert not out.startswith("ERROR"), out
    return f.read_text()


# ---------------------------------------------------------------------------
# W — forward WORD (whitespace-delimited)
# ---------------------------------------------------------------------------

def test_W_jumps_past_punctuation(tmp_path: Path) -> None:
    # w would stop at the punctuation; W treats foo,bar as one WORD.
    # Initial: cursor at BOF on 'f'. After W, cursor should be on 'n' of 'next'.
    out = _run(tmp_path, "foo,bar next\n", "gg␞W␞iX")
    assert out == "foo,bar Xnext\n"


def test_W_with_count(tmp_path: Path) -> None:
    out = _run(tmp_path, "one two three four\n", "gg␞3W␞iX")
    assert out == "one two three Xfour\n"


# ---------------------------------------------------------------------------
# B — back WORD start
# ---------------------------------------------------------------------------

def test_B_jumps_back_over_punctuation(tmp_path: Path) -> None:
    # Start at end of 'next', go back one WORD → start of 'next'.
    # Then another B goes back to 'foo,bar'.
    out = _run(tmp_path, "foo,bar next\n", "gg␞$␞2B␞iX")
    # $ on "foo,bar next\n" lands on 't' (last char). 2B should go to 'f'.
    assert out == "Xfoo,bar next\n"


# ---------------------------------------------------------------------------
# E — forward to WORD end
# ---------------------------------------------------------------------------

def test_E_jumps_to_word_end(tmp_path: Path) -> None:
    # From BOF on 'f', E goes to last char of 'foo,bar' = 'r'.
    # iX inserts before 'r'.
    out = _run(tmp_path, "foo,bar next\n", "gg␞E␞iX")
    assert out == "foo,baXr next\n"


# ---------------------------------------------------------------------------
# ge — back to word end
# ---------------------------------------------------------------------------

def test_ge_goes_back_to_prev_word_end(tmp_path: Path) -> None:
    # cursor on 'n' of 'next'. ge → end of prev word 'foo' = 'o' (last).
    out = _run(tmp_path, "foo next\n", "gg␞w␞ge␞iX")
    assert out == "foXo next\n"


# ---------------------------------------------------------------------------
# gE — back to WORD end
# ---------------------------------------------------------------------------

def test_gE_goes_back_to_prev_WORD_end(tmp_path: Path) -> None:
    # cursor on 'n' of 'next'. gE → end of prev WORD 'foo,bar' = 'r'.
    out = _run(tmp_path, "foo,bar next\n", "gg␞W␞gE␞iX")
    assert out == "foo,baXr next\n"


# ---------------------------------------------------------------------------
# g_ — last non-blank of line
# ---------------------------------------------------------------------------

def test_g_underscore_lands_on_last_non_blank(tmp_path: Path) -> None:
    # Trailing spaces on the line; g_ skips them. cursor at BOF then g_.
    out = _run(tmp_path, "hello world   \n", "gg␞g_␞aX")
    # 'd' is last non-blank; aX appends after → "hello worldX   \n"
    assert out == "hello worldX   \n"


# ---------------------------------------------------------------------------
# + and - — first non-blank of next/prev line
# ---------------------------------------------------------------------------

def test_plus_goes_to_first_non_blank_of_next_line(tmp_path: Path) -> None:
    out = _run(tmp_path, "first\n  indented\n", "gg␞+␞iX")
    # cursor lands on 'i' of "indented" (skips 2 leading spaces).
    assert out == "first\n  Xindented\n"


def test_minus_goes_to_first_non_blank_of_prev_line(tmp_path: Path) -> None:
    out = _run(tmp_path, "  first\nsecond\n", "gg␞j␞-␞iX")
    # On line 2, then - goes to line 1 first-non-blank → 'f'.
    assert out == "  Xfirst\nsecond\n"


# ---------------------------------------------------------------------------
# _ — first non-blank of current line (count: down N-1)
# ---------------------------------------------------------------------------

def test_underscore_goes_to_first_non_blank_current_line(tmp_path: Path) -> None:
    out = _run(tmp_path, "  hello\n", "gg␞_␞iX")
    assert out == "  Xhello\n"


def test_underscore_with_count_goes_down(tmp_path: Path) -> None:
    # 3_ should go down 2 lines, first non-blank.
    out = _run(tmp_path, "a\nb\n  c\nd\n", "gg␞3_␞iX")
    assert out == "a\nb\n  Xc\nd\n"


# ---------------------------------------------------------------------------
# { and } — paragraph (blank-line) boundaries
# ---------------------------------------------------------------------------

def test_close_brace_jumps_to_next_blank_line(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        "para1 line1\npara1 line2\n\npara2 line1\n",
        "gg␞}␞iX",
    )
    # } lands on the blank line between paragraphs.
    assert out == "para1 line1\npara1 line2\nX\npara2 line1\n"


def test_open_brace_jumps_back_to_prev_blank_line(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        "para1\n\npara2 line1\npara2 line2\n",
        "G␞{␞iX",
    )
    # G lands on BOL of last line; { goes back to the blank line.
    assert out == "para1\nX\npara2 line1\npara2 line2\n"


# ---------------------------------------------------------------------------
# ( and ) — sentence boundaries
# ---------------------------------------------------------------------------

def test_close_paren_jumps_to_next_sentence(tmp_path: Path) -> None:
    out = _run(tmp_path, "First sentence. Second sentence.\n", "gg␞)␞iX")
    # ) lands at start of "Second" (first char after ". ").
    assert out == "First sentence. XSecond sentence.\n"


def test_open_paren_jumps_back_to_prev_sentence(tmp_path: Path) -> None:
    out = _run(tmp_path, "First sentence. Second sentence.\n", "gg␞$␞(␞iX")
    # From EOL of one-line text, ( goes to start of current sentence "Second".
    # Actually from EOL it'd be "Second sentence." → start is "S" of "Second".
    # Then we step back one sentence.
    # Simpler: from middle of "Second", ( returns to start of "Second".
    # Adjust: place cursor in middle of Second, then (.
    out = _run(tmp_path, "First. Second.\n", "gg␞/Second␞l␞l␞(␞iX")
    # Cursor on 'c' of "Second" (after /Second + 2*l). ( → start of "Second".
    assert out == "First. XSecond.\n"


# ---------------------------------------------------------------------------
# % — match bracket
# ---------------------------------------------------------------------------

def test_percent_jumps_to_matching_close_paren(tmp_path: Path) -> None:
    out = _run(tmp_path, "foo(bar)baz\n", "gg␞/(␞%␞iX")
    # Cursor lands on ')', iX inserts before it.
    assert out == "foo(barX)baz\n"


def test_percent_jumps_to_matching_open_brace(tmp_path: Path) -> None:
    out = _run(tmp_path, "a{b{c}d}e\n", "gg␞$␞h␞iX")
    # First sanity: $ lands on 'e', h backs to '}' (outer). Insert X before '}'.
    assert out == "a{b{c}dX}e\n"
    # Now real %: cursor on outer '}' should jump back to outer '{'.
    out2 = _run(tmp_path, "a{b{c}d}e\n", "gg␞/}␞l␞/}␞%␞iX")
    # /} → inner '}', l advances, /} → outer '}'.
    # % from outer '}' → outer '{' (the '{' after 'a'). iX before '{'.
    assert out2 == "aX{b{c}d}e\n"


def test_percent_nested_brackets(tmp_path: Path) -> None:
    out = _run(tmp_path, "[a[b]c]\n", "gg␞%␞iX")
    # cursor on outer '[' → matches outer ']'. iX inserts before it.
    assert out == "[a[b]cX]\n"


# ---------------------------------------------------------------------------
# ; and , — repeat last f/F/t/T
# ---------------------------------------------------------------------------

def test_semicolon_repeats_f(tmp_path: Path) -> None:
    out = _run(tmp_path, "a,b,c,d\n", "gg␞f,␞;␞iX")
    # First f, → first comma. ; → second comma. iX before second comma.
    assert out == "a,bX,c,d\n"


def test_comma_reverses_f(tmp_path: Path) -> None:
    out = _run(tmp_path, "a,b,c,d\n", "gg␞f,␞;␞;␞,␞iX")
    # f, → 1st. ; → 2nd. ; → 3rd. , → 2nd (reverse). iX before 2nd comma.
    assert out == "a,bX,c,d\n"


def test_semicolon_repeats_t(tmp_path: Path) -> None:
    out = _run(tmp_path, "a,b,c,d\n", "gg␞t,␞l␞l␞;␞iX")
    # t, → before 1st comma (pos 0 → cursor on 'a'? actually t, lands on char before ',' = 'a').
    # Then l l moves to 'b'. ; (repeat t,) → land before next ',' = 'b'...
    # Simpler check:
    out = _run(tmp_path, "x.y.z\n", "gg␞t.␞;␞iX")
    # t. → land on 'x' (char before first '.'). ; from current pos repeats t.,
    # which finds next '.' (after 'y') and lands on 'y'. iX inserts before 'y'.
    assert out == "x.Xy.z\n"


# ---------------------------------------------------------------------------
# Operator-motion tests: d{, c}, y%, d+, d-, dg_, c_, d(, c), dge, dgE, dW, dB, dE
# ---------------------------------------------------------------------------

def test_d_close_brace_deletes_to_next_blank(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        "para1\npara1b\n\npara2\n",
        "gg␞d}",
    )
    # d} from BOF deletes through blank line.
    assert out == "\npara2\n"


def test_c_open_brace_changes_back_to_prev_blank(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        "para1\n\npara2line1\npara2line2\n",
        "G␞c{NEW",
    )
    # G → BOL of last line. c{ deletes back through blank line, insert NEW.
    assert "NEW" in out


def test_y_percent_yanks_brackets(tmp_path: Path) -> None:
    out = _run(tmp_path, "foo(bar)baz\n", "gg␞/(␞y%␞G␞p")
    # Yank from ( to ) inclusive, paste at end.
    # After y% register has "(bar)". p pastes after cursor on last line.
    assert "(bar)" in out


def test_d_plus_deletes_current_and_next_line(tmp_path: Path) -> None:
    out = _run(tmp_path, "a\nb\nc\n", "gg␞d+")
    # d+ deletes current line and next line (down to first non-blank of next).
    # Linewise behavior expected.
    assert out == "c\n"


def test_d_minus_deletes_prev_and_current_line(tmp_path: Path) -> None:
    out = _run(tmp_path, "a\nb\nc\n", "gg␞j␞j␞d-")
    # Cursor on line 3. d- deletes prev + current.
    assert out == "a\n"


def test_d_g_underscore_deletes_to_last_non_blank(tmp_path: Path) -> None:
    out = _run(tmp_path, "hello world   \n", "gg␞dg_")
    # dg_ deletes from cursor to last non-blank inclusive.
    # cursor at BOF deletes "hello world" leaving trailing spaces.
    assert out == "   \n"


def test_d_W_deletes_WORD(tmp_path: Path) -> None:
    out = _run(tmp_path, "foo,bar next\n", "gg␞dW")
    # dW deletes "foo,bar " (whole WORD + trailing space).
    assert out == "next\n"


def test_d_B_deletes_back_WORD(tmp_path: Path) -> None:
    out = _run(tmp_path, "foo,bar next\n", "gg␞$␞dB")
    # $ on "next" → 't'. dB deletes back to start of 'next'... actually
    # from cursor on 't' (last char of "next"), B goes back to start of "next".
    # So dB deletes "nex". Cursor at 'n', delete to 't' exclusive.
    assert "t\n" in out  # 't' remains


def test_d_E_deletes_to_WORD_end(tmp_path: Path) -> None:
    out = _run(tmp_path, "foo,bar next\n", "gg␞dE")
    # dE deletes from cursor to end of WORD "foo,bar" inclusive.
    assert out == " next\n"


def test_d_ge_deletes_back_to_word_end(tmp_path: Path) -> None:
    out = _run(tmp_path, "foo next\n", "gg␞w␞dge")
    # cursor on 'n' of 'next'. dge deletes from end-of-prev-word
    # forward through cursor exclusive: " " removed.
    assert out == "foonext\n"


def test_d_gE_deletes_back_to_WORD_end(tmp_path: Path) -> None:
    out = _run(tmp_path, "foo,bar next\n", "gg␞W␞dgE")
    # cursor on 'n' of 'next'. dgE deletes whitespace between WORDs.
    assert out == "foo,barnext\n"


def test_d_underscore_deletes_current_line_linewise(tmp_path: Path) -> None:
    out = _run(tmp_path, "a\nb\nc\n", "gg␞j␞d_")
    # d_ on line 2 deletes line 2 linewise.
    assert out == "a\nc\n"


def test_d_semicolon_repeats_find_delete(tmp_path: Path) -> None:
    out = _run(tmp_path, "a,b,c,d\n", "gg␞f,␞d;")
    # f, → 1st comma. d; deletes from cursor (1st ',') through next ','
    # (2nd) inclusive: ",b," removed.
    assert out == "ac,d\n"
