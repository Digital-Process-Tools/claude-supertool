"""Tests for op_edit_session — cursor-based multi-action edit op."""
from __future__ import annotations

from pathlib import Path

import supertool


# ---------------------------------------------------------------------------
# Single-action sanity
# ---------------------------------------------------------------------------

def test_insert_at_line_col(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("hello\nworld\n")
    out = supertool.op_edit_session(str(f), "@1:6;+!")
    assert "edit_session" in out
    assert f.read_text() == "hello!\nworld\n"


def test_delete_chars(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("abcdef\n")
    out = supertool.op_edit_session(str(f), "@1:2;-3")
    assert "edit_session" in out
    assert f.read_text() == "aef\n"


# ---------------------------------------------------------------------------
# Multi-action — coordinate stability
# ---------------------------------------------------------------------------

def test_multiple_inserts_keep_coordinates_valid(tmp_path: Path) -> None:
    """After inserting ZZZ\\n at top, the original line 5 ('e') is now at
    line 6 in the live buffer — coordinates address the live buffer, the
    caller sequences accordingly."""
    f = tmp_path / "x.py"
    f.write_text("a\nb\nc\nd\ne\n")
    # \\n is a literal backslash-n in the script — decoded to a real newline
    # by op_edit_session, so it doesn't terminate the +TEXT action.
    out = supertool.op_edit_session(str(f), "@1:1;+ZZZ\\n;@6:1;+!")
    assert f.read_text() == "ZZZ\na\nb\nc\nd\n!e\n"


def test_semicolon_and_newline_separators(tmp_path: Path) -> None:
    """Both ';' and real '\\n' split actions in the script."""
    f = tmp_path / "x.py"
    f.write_text("x\n")
    out = supertool.op_edit_session(str(f), "@1:1;+a\n@1:3;+b")
    # Buffer: "x\n" → @1:1+a → "ax\n" → @1:3 (col 3 of "ax\n" = before \n) +b → "axb\n"
    assert f.read_text() == "axb\n"


def test_three_actions(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("foo\n")
    out = supertool.op_edit_session(str(f), "@1:1;+(;@1:5;+)")
    # @1:1 → insert "(" → "(foo\n", cursor at 1
    # @1:5 → in updated buffer "(foo\n", line 1 col 5 = position of '\n' (after o)
    # Insert ")" → "(foo)\n"
    assert f.read_text() == "(foo)\n"


# ---------------------------------------------------------------------------
# Escapes
# ---------------------------------------------------------------------------

def test_insert_with_newline_escape(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("ab\n")
    out = supertool.op_edit_session(str(f), "@1:2;+\\n")
    assert f.read_text() == "a\nb\n"


def test_insert_with_tab_escape(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("ab\n")
    out = supertool.op_edit_session(str(f), "@1:2;+\\t")
    assert f.read_text() == "a\tb\n"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

def test_missing_file(tmp_path: Path) -> None:
    out = supertool.op_edit_session(str(tmp_path / "nope.py"), "@1:1;+x")
    assert "ERROR: file not found" in out


def test_empty_script(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("a\n")
    out = supertool.op_edit_session(str(f), "")
    assert "ERROR: empty script" in out


def test_empty_path(tmp_path: Path) -> None:
    out = supertool.op_edit_session("", "@1:1;+x")
    assert "ERROR: empty path" in out


def test_unknown_action(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("a\n")
    out = supertool.op_edit_session(str(f), "?bogus")
    assert "ERROR" in out and "unknown action" in out
    # File untouched
    assert f.read_text() == "a\n"


def test_goto_beyond_eof(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("a\n")
    out = supertool.op_edit_session(str(f), "@99:1;+x")
    assert "ERROR" in out and "beyond EOF" in out


def test_delete_past_eof(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("ab\n")
    out = supertool.op_edit_session(str(f), "@1:1;-99")
    assert "ERROR" in out and "delete past EOF" in out


def test_negative_delete_count(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("ab\n")
    out = supertool.op_edit_session(str(f), "@1:1;--5")
    assert "ERROR" in out


def test_non_integer_delete_count(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("ab\n")
    out = supertool.op_edit_session(str(f), "@1:1;-xx")
    assert "ERROR" in out and "integer" in out


def test_malformed_goto(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("ab\n")
    out = supertool.op_edit_session(str(f), "@5")
    assert "ERROR" in out and "@LINE:COL" in out


# ---------------------------------------------------------------------------
# Dispatch (triple-colon)
# ---------------------------------------------------------------------------

def test_dispatch_triple_colon(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("foo\n")
    arg = f"edit_session:::{f}:::@1:1;+(;@1:5;+)"
    out = supertool.dispatch(arg)
    assert "edit_session" in out
    assert f.read_text() == "(foo)\n"


def test_dispatch_missing_script(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("foo\n")
    out = supertool.dispatch(f"edit_session:::{f}:::")
    assert "ERROR: empty script" in out


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------

def test_receipt_shows_cursor_and_context(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("a\nb\nc\nd\ne\n")
    out = supertool.op_edit_session(str(f), "@3:1;+X")
    assert "cursor at 3:" in out
    assert "--- context ---" in out
    assert "→" in out  # marker for cursor line


# ---------------------------------------------------------------------------
# Weird chars — UTF-8, emoji, escapes, regex meta
# ---------------------------------------------------------------------------

def test_insert_emoji(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("hello\n")
    out = supertool.op_edit_session(str(f), "@1:6;+ 🦫")
    assert "edit_session" in out
    assert f.read_text() == "hello 🦫\n"


def test_insert_accented_chars(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("cafe\n")
    out = supertool.op_edit_session(str(f), "@1:4;-1;+é")
    assert f.read_text() == "café\n"


def test_cursor_position_after_multibyte(tmp_path: Path) -> None:
    """Col is char-indexed, not byte-indexed. After 'café' (4 chars, 5 bytes
    in UTF-8), col 5 is the position right after the 'é'."""
    f = tmp_path / "x.py"
    f.write_text("café\n")
    out = supertool.op_edit_session(str(f), "@1:5;+!")
    assert f.read_text() == "café!\n"


def test_insert_chinese(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("hi\n")
    out = supertool.op_edit_session(str(f), "@1:3;+ 你好")
    assert f.read_text() == "hi 你好\n"


def test_insert_zero_width_joiner(tmp_path: Path) -> None:
    """ZWJ-composed emoji (e.g., 👨‍💻 = man + ZWJ + laptop) — multiple code
    points, but Python str treats each code point as one char."""
    f = tmp_path / "x.py"
    f.write_text("dev:\n")
    zwj_emoji = "👨‍💻"
    out = supertool.op_edit_session(str(f), f"@1:5;+ {zwj_emoji}")
    assert zwj_emoji in f.read_text()


def test_insert_with_quotes_and_backslash(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("x\n")
    out = supertool.op_edit_session(str(f), "@1:1;+\"a'b\\\\c")
    # The +TEXT goes through _decode_escapes: \\\\ → \\ → actual single backslash
    assert f.read_text() == "\"a'b\\cx\n"


def test_insert_regex_meta_chars(tmp_path: Path) -> None:
    """edit_session is char-based, not regex — meta chars are inserted literally."""
    f = tmp_path / "x.py"
    f.write_text("x\n")
    out = supertool.op_edit_session(str(f), "@1:1;+.*+?[]()$^")
    assert f.read_text() == ".*+?[]()$^x\n"


def test_insert_explicit_newline_escape(tmp_path: Path) -> None:
    """\\n in +TEXT decodes to a real newline — splits the line in two."""
    f = tmp_path / "x.py"
    f.write_text("ab\n")
    out = supertool.op_edit_session(str(f), "@1:2;+\\n")
    assert f.read_text() == "a\nb\n"


def test_insert_explicit_carriage_return_escape(tmp_path: Path) -> None:
    """Path.read_text() does universal-newline translation (\\r → \\n) — use
    read_bytes() to verify the actual byte hit disk."""
    f = tmp_path / "x.py"
    f.write_text("ab\n")
    out = supertool.op_edit_session(str(f), "@1:2;+\\r")
    assert f.read_bytes() == b"a\rb\n"


def test_delete_multibyte_chars(tmp_path: Path) -> None:
    """Delete count is char-indexed: 'café' → delete 2 from col 3 removes 'fé'."""
    f = tmp_path / "x.py"
    f.write_text("café\n")
    out = supertool.op_edit_session(str(f), "@1:3;-2")
    assert f.read_text() == "ca\n"


def test_insert_preserves_existing_utf8(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("café\nzürich\n")
    out = supertool.op_edit_session(str(f), "@2:1;+!")
    assert f.read_text() == "café\n!zürich\n"


def test_dispatch_with_emoji_in_script(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("test\n")
    out = supertool.dispatch(f"edit_session:::{f}:::@1:5;+ 🚀")
    assert "edit_session" in out
    assert f.read_text() == "test 🚀\n"


# ---------------------------------------------------------------------------
# /PATTERN — find from cursor
# ---------------------------------------------------------------------------

def test_find_pattern_moves_cursor(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("foo bar baz\n")
    out = supertool.op_edit_session(str(f), "/bar;+!")
    assert f.read_text() == "foo !bar baz\n"


def test_find_lands_at_match_start_not_end(tmp_path: Path) -> None:
    """/ lands at the FIRST char of the match (vim-like). To advance past
    the match, compose with insert/delete or anchors. Successive /X without
    advance will find the SAME match — by design."""
    f = tmp_path / "x.py"
    f.write_text("xx yy xx\n")
    out = supertool.op_edit_session(str(f), "/xx;+a;/xx;+b")
    # /xx → cursor=0; +a → "axx yy xx\n", cursor=1
    # /xx from cursor=1 → finds "xx" at idx=1 (still there); +b → "abxx yy xx\n"
    assert f.read_text() == "abxx yy xx\n"


def test_find_skip_via_pattern_length_advance(tmp_path: Path) -> None:
    """To skip past the current match: insert+delete or use anchors. Here:
    after first /xx;+!, cursor sits between '!' and 'xx', so next /xx finds
    the same match. Use $ to jump to EOL between finds for true skip."""
    f = tmp_path / "x.py"
    f.write_text("xx yy xx\n")
    out = supertool.op_edit_session(str(f), "/xx;$;/xx;+!")
    # /xx → cursor=0; $ → EOL=8; /xx from 8 → not found
    assert "ERROR" in out and "not found" in out


def test_find_pattern_not_found(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("foo\n")
    out = supertool.op_edit_session(str(f), "/missing;+!")
    assert "ERROR" in out and "not found" in out
    assert f.read_text() == "foo\n"


def test_find_empty_pattern(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("foo\n")
    out = supertool.op_edit_session(str(f), "/")
    assert "ERROR" in out and "empty search pattern" in out


def test_find_pattern_with_escaped_newline(tmp_path: Path) -> None:
    """Escaped \\n in pattern matches a real newline."""
    f = tmp_path / "x.py"
    f.write_text("a\nb\n")
    out = supertool.op_edit_session(str(f), "/a\\nb;+X")
    # Cursor at start of "a\nb", insert X before
    assert f.read_text() == "Xa\nb\n"


def test_find_pattern_unicode(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("hello café world\n")
    out = supertool.op_edit_session(str(f), "/café;+ tasty")
    assert f.read_text() == "hello  tastycafé world\n"


# ---------------------------------------------------------------------------
# $ / ^ — line anchors
# ---------------------------------------------------------------------------

def test_dollar_jumps_to_eol(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("foo\nbar\n")
    out = supertool.op_edit_session(str(f), "@1:1;$;+!")
    assert f.read_text() == "foo!\nbar\n"


def test_caret_jumps_to_bol(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("foo\nbar\n")
    out = supertool.op_edit_session(str(f), "@2:3;^;+!")
    assert f.read_text() == "foo\n!bar\n"


def test_dollar_on_last_line_no_trailing_newline(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("foo")
    out = supertool.op_edit_session(str(f), "$;+!")
    assert f.read_text() == "foo!"


def test_caret_at_offset_zero(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("foo\n")
    out = supertool.op_edit_session(str(f), "^;+!")
    assert f.read_text() == "!foo\n"


def test_combined_find_eol_append(tmp_path: Path) -> None:
    """The killer pattern: find function, append at end of that line."""
    f = tmp_path / "x.py"
    f.write_text("def foo():\n    pass\n")
    out = supertool.op_edit_session(str(f), "/def foo();$;+  # marker")
    assert f.read_text() == "def foo():  # marker\n    pass\n"


# ---------------------------------------------------------------------------
# < / > — relative cursor moves
# ---------------------------------------------------------------------------

def test_left_move_basic(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("abcdef\n")
    out = supertool.op_edit_session(str(f), "@1:5;<2;+!")
    # @1:5 → cursor=4 (between 'd' and 'e'); <2 → cursor=2; +! → "ab!cdef\n"
    assert f.read_text() == "ab!cdef\n"


def test_right_move_basic(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("abcdef\n")
    out = supertool.op_edit_session(str(f), "@1:1;>3;+!")
    # cursor=0; >3 → cursor=3; +! → "abc!def\n"
    assert f.read_text() == "abc!def\n"


def test_left_clamps_to_zero(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("abc\n")
    out = supertool.op_edit_session(str(f), "@1:2;<99;+!")
    # cursor=1; <99 → clamps to 0; +! → "!abc\n"
    assert f.read_text() == "!abc\n"


def test_right_clamps_to_eof(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("abc")  # no trailing newline
    out = supertool.op_edit_session(str(f), "@1:1;>99;+!")
    assert f.read_text() == "abc!"


def test_find_then_left_then_insert(tmp_path: Path) -> None:
    """The exact case asked for: /def, then 3 left, then append."""
    f = tmp_path / "x.py"
    f.write_text("xxxdef foo():\n")
    out = supertool.op_edit_session(str(f), "/def;<3;+!")
    # /def → cursor=3; <3 → cursor=0; +! → "!xxxdef foo():\n"
    assert f.read_text() == "!xxxdef foo():\n"


def test_left_non_integer(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("abc\n")
    out = supertool.op_edit_session(str(f), "<xx")
    assert "ERROR" in out and "integer" in out


def test_right_negative(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("abc\n")
    out = supertool.op_edit_session(str(f), ">-3")
    assert "ERROR" in out


def test_bof_anchor(tmp_path: Path) -> None:
    """^^ jumps to start of file regardless of current cursor."""
    f = tmp_path / "x.py"
    f.write_text("a\nb\nc\n")
    out = supertool.op_edit_session(str(f), "@3:1;^^;+!")
    assert f.read_text() == "!a\nb\nc\n"


def test_eof_anchor(tmp_path: Path) -> None:
    """$$ jumps to end of file (after the last char)."""
    f = tmp_path / "x.py"
    f.write_text("a\nb\nc\n")
    out = supertool.op_edit_session(str(f), "$$;+END")
    assert f.read_text() == "a\nb\nc\nEND"


def test_eof_then_append_block(tmp_path: Path) -> None:
    """Append a multi-line block at end of file via $$ + escaped \\n."""
    f = tmp_path / "x.py"
    f.write_text("first\n")
    out = supertool.op_edit_session(str(f), "$$;+second\\nthird\\n")
    assert f.read_text() == "first\nsecond\nthird\n"


def test_bof_anchor_on_empty_file(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("")
    out = supertool.op_edit_session(str(f), "^^;+hello")
    assert f.read_text() == "hello"


def test_double_anchor_distinct_from_single(tmp_path: Path) -> None:
    """^^ ≠ ^: ^ goes to start of CURRENT line, ^^ goes to start of FILE."""
    f = tmp_path / "x.py"
    f.write_text("aaa\nbbb\nccc\n")
    out_single = supertool.op_edit_session(str(f), "@2:3;^;+X")
    assert f.read_text() == "aaa\nXbbb\nccc\n"

    f.write_text("aaa\nbbb\nccc\n")
    out_double = supertool.op_edit_session(str(f), "@2:3;^^;+X")
    assert f.read_text() == "Xaaa\nbbb\nccc\n"


def test_row_up_basic(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("aaa\nbbb\nccc\n")
    out = supertool.op_edit_session(str(f), "@3:2;k1;+!")
    # @3:2 → cursor on 'b' of bbb? No — line 3 col 2 = pos in "ccc"
    # Wait line 3 = "ccc", col 2 = between c[0] and c[1]
    # k1 → up 1 row, line 2 col 2 = between b[0] and b[1]
    # +! → "aaa\nb!bb\nccc\n"
    assert f.read_text() == "aaa\nb!bb\nccc\n"


def test_row_down_basic(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("aaa\nbbb\nccc\n")
    out = supertool.op_edit_session(str(f), "@1:2;j1;+!")
    assert f.read_text() == "aaa\nb!bb\nccc\n"


def test_row_up_clamps_to_first_line(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("aaa\nbbb\nccc\n")
    out = supertool.op_edit_session(str(f), "@3:1;k99;+!")
    assert f.read_text() == "!aaa\nbbb\nccc\n"


def test_row_down_clamps_to_last_line(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("aaa\nbbb\nccc\n")
    out = supertool.op_edit_session(str(f), "@1:1;j99;+!")
    # Last line in "aaa\nbbb\nccc\n" is 4 (the empty line after final \n)
    # col 1 of empty line = offset at end of file
    assert f.read_text() == "aaa\nbbb\nccc\n!"


def test_row_move_preserves_column(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("aaaaa\nbbbbb\nccccc\n")
    out = supertool.op_edit_session(str(f), "@1:4;j2;+!")
    # Col 4 preserved across rows; line 3 col 4 → "ccc!cc\n"
    assert f.read_text() == "aaaaa\nbbbbb\nccc!cc\n"


def test_row_move_column_clamps_on_short_line(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("aaaaaaaa\nbb\ncccccccc\n")
    out = supertool.op_edit_session(str(f), "@1:8;j1;+!")
    # Col 8 on a 2-char line → clamps to end of line "bb" (col 3)
    assert f.read_text() == "aaaaaaaa\nbb!\ncccccccc\n"


def test_row_combined_with_dollar(tmp_path: Path) -> None:
    """Common combo: jump down N rows then to end of line."""
    f = tmp_path / "x.py"
    f.write_text("a\nb\nc\nd\n")
    out = supertool.op_edit_session(str(f), "j2;$;+!")
    # cursor=0; j2 → line 3 (preserves col 1, "c\n", offset 4); $ → eol of line 3 (offset 5); +!
    assert f.read_text() == "a\nb\nc!\nd\n"


def test_row_non_integer(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("a\nb\n")
    out = supertool.op_edit_session(str(f), "kxx")
    assert "ERROR" in out and "integer" in out


def test_left_unicode_chars(tmp_path: Path) -> None:
    """Move counts in chars, not bytes — multi-byte UTF-8 stays consistent."""
    f = tmp_path / "x.py"
    f.write_text("café_end\n")
    # @1:5 lands cursor right after 'é' (4 chars in). <2 → between 'a' and 'f'.
    out = supertool.op_edit_session(str(f), "@1:5;<2;+!")
    assert f.read_text() == "ca!fé_end\n"
