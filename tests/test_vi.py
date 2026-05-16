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
# Change verbs (ciw, cw, cc, ci<delim>)
# ---------------------------------------------------------------------------

def test_ciw_replaces_word(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("foo bar baz\n")
    supertool.op_vi(str(f), "/bar;ciwQUUX")
    assert f.read_text() == "foo QUUX baz\n"


def test_ciw_on_non_word_errors(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("foo bar\n")
    out = supertool.op_vi(str(f), "/ ;ciwX")
    assert "ERROR" in out


def test_cw_changes_word_from_cursor(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("foo bar baz\n")
    supertool.op_vi(str(f), "/bar;l;cwAR")
    assert f.read_text() == "foo bAR baz\n"


def test_cc_replaces_line(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("line one\nline two\nline three\n")
    supertool.op_vi(str(f), "/two;ccLINE TWO")
    assert f.read_text() == "line one\nLINE TWO\nline three\n"


def test_cc_count_replaces_n_lines(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("a\nb\nc\nd\n")
    supertool.op_vi(str(f), "gg;2ccXY")
    assert f.read_text() == "XY\nc\nd\n"


def test_ci_double_quote(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text('label = "old text"\n')
    supertool.op_vi(str(f), '/";ci"new text')
    assert f.read_text() == 'label = "new text"\n'


def test_ci_single_quote(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("name = 'alice'\n")
    supertool.op_vi(str(f), "/';ci'bob")
    assert f.read_text() == "name = 'bob'\n"


def test_ci_paren_nested(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("foo(bar(x), y)\n")
    supertool.op_vi(str(f), "/foo(;ci(NEW")
    assert f.read_text() == "foo(NEW)\n"


def test_ci_bracket(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("a = [1, 2, 3]\n")
    supertool.op_vi(str(f), "/[;ci[9, 9, 9")
    assert f.read_text() == "a = [9, 9, 9]\n"


def test_ci_brace(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("d = {a: 1}\n")
    supertool.op_vi(str(f), "/{;ci{b: 2")
    assert f.read_text() == "d = {b: 2}\n"


def test_ci_no_opener_errors(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("no delim here\n")
    out = supertool.op_vi(str(f), 'G;ci"X')
    assert "ERROR" in out


# ---------------------------------------------------------------------------
# Join
# ---------------------------------------------------------------------------

def test_J_joins_two_lines(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("foo\nbar\nbaz\n")
    supertool.op_vi(str(f), "gg;J")
    assert f.read_text() == "foo bar\nbaz\n"


def test_J_count_joins_n_lines(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("a\nb\nc\nd\n")
    supertool.op_vi(str(f), "gg;3J")
    assert f.read_text() == "a b c d\n"


def test_J_strips_leading_whitespace(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("foo\n    bar\n")
    supertool.op_vi(str(f), "gg;J")
    assert f.read_text() == "foo bar\n"


# ---------------------------------------------------------------------------
# Escape sequences (\; literal semicolon, \\ literal backslash)
# ---------------------------------------------------------------------------

def test_semicolon_escape_in_insert(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("x = 1\n")
    supertool.op_vi(str(f), "$;a\\; y = 2")
    assert f.read_text() == "x = 1; y = 2\n"


def test_semicolon_escape_in_ciw(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("FOO bar\n")
    supertool.op_vi(str(f), "/FOO;ciwa\\;b\\;c")
    assert f.read_text() == "a;b;c bar\n"


def test_backslash_escape_in_insert(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("\n")
    supertool.op_vi(str(f), "ipath\\\\to\\\\file")
    assert f.read_text() == "path\\to\\file\n"


def test_bang_history_escape_is_flattened(tmp_path: Path) -> None:
    """zsh/bash insert `\\!` even inside single quotes — strip the backslash."""
    f = tmp_path / "x.py"
    f.write_text("\n")
    supertool.op_vi(str(f), "iif (\\!$x->isOk()) {}")
    assert f.read_text() == "if (!$x->isOk()) {}\n"



# ---------------------------------------------------------------------------
# Regex search
# ---------------------------------------------------------------------------

def test_regex_search_char_class(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("count = 42\n")
    supertool.op_vi(str(f), "/[0-9]+;ciw99")
    assert f.read_text() == "count = 99\n"


def test_regex_search_anchor(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("first line\nsecond line\nthird line\n")
    supertool.op_vi(str(f), "/^second;cw2ND")
    assert f.read_text() == "first line\n2ND line\nthird line\n"


def test_regex_search_alternation(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("apple banana cherry\n")
    supertool.op_vi(str(f), "/banana|cherry;ciwPEAR")
    assert f.read_text() == "apple PEAR cherry\n"


def test_regex_multiline_match(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("start\nmiddle\nend\n")
    supertool.op_vi(str(f), "/middle\\nend;D")
    # cursor lands on 'm' of middle, D deletes to EOL of cursor line only
    assert f.read_text() == "start\n\nend\n"


def test_regex_backward(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("foo1 foo2 foo3\n")
    supertool.op_vi(str(f), "G;?foo[0-9];ciwLAST")
    assert f.read_text() == "foo1 foo2 LAST\n"


def test_sed_style_pat_cmd_auto_splits_on_miss(tmp_path: Path) -> None:
    """Kevin reflex `/PAT/CMD` (sed-style) auto-splits when strict search misses
    and the shortened pattern matches. Trailing `<CMD>` becomes the next action."""
    f = tmp_path / "x.php"
    f.write_text("<?php\n/**\n * @coverage 85%\n */\nclass Foo {}\n")
    supertool.op_vi(str(f), '/ \\* @coverage/O * @coverageAuditWarn "x"')
    assert "@coverageAuditWarn" in f.read_text().splitlines()[2]


def test_sed_style_auto_split_does_not_hijack_legit_slash(tmp_path: Path) -> None:
    """If the full pattern with embedded `/<verb>` matches, no auto-split."""
    f = tmp_path / "x.txt"
    f.write_text("/usr/Open at top\n")
    # Auto-split shouldn't fire because strict search finds 'usr/Op' literally
    # in the file. Cursor lands at the 'u' (start of match), `i!` inserts there.
    supertool.op_vi(str(f), "/usr/Op;i!")
    assert f.read_text() == "/!usr/Open at top\n"


def test_regex_special_char_literal_fallback(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("a = foo(bar)\n")
    # `foo(` is invalid regex (unbalanced paren) → falls back to literal find
    supertool.op_vi(str(f), "/foo(;ci(NEW")
    assert f.read_text() == "a = foo(NEW)\n"


# ---------------------------------------------------------------------------
# Char-find on line: f F t T
# ---------------------------------------------------------------------------

def test_f_finds_char_forward(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("abc=def\n")
    supertool.op_vi(str(f), "fd;i!")
    assert f.read_text() == "abc=!def\n"


def test_F_finds_char_backward(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("abc=def\n")
    supertool.op_vi(str(f), "$;Fa;i!")
    assert f.read_text() == "!abc=def\n"


def test_t_stops_before_char(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("abcd\n")
    # `tc` lands cursor on `b` (1 char before target `c`); `i!` inserts before
    supertool.op_vi(str(f), "tc;i!")
    assert f.read_text() == "a!bcd\n"


def test_T_stops_after_char_backward(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("abcd\n")
    # `$` lands on `d`; `Ta` lands on `b` (1 char after target `a`); `i!` inserts before
    supertool.op_vi(str(f), "$;Ta;i!")
    assert f.read_text() == "a!bcd\n"


# ---------------------------------------------------------------------------
# Repeat search: n N
# ---------------------------------------------------------------------------

def test_n_repeats_forward_search(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("foo foo foo\n")
    supertool.op_vi(str(f), "/foo;n;ciwBAR")
    assert f.read_text() == "foo BAR foo\n"


def test_N_reverses_last_search(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("foo bar foo bar\n")
    supertool.op_vi(str(f), "G;?foo;N;ciwLAST")
    # ?foo lands on 2nd foo (backward from EOF); N reverses → forward → next foo... none.
    # actually only 2 foos; backward to 2nd, N forward → no further match → error
    out = supertool.op_vi(str(f), "/foo;n;N;ciwFIRST")
    # /foo → 1st, n → 2nd, N → 1st again. ciwFIRST replaces 1st foo
    assert f.read_text() == "FIRST bar foo bar\n"


# ---------------------------------------------------------------------------
# Change to motion: c$ c0
# ---------------------------------------------------------------------------

def test_c_dollar_changes_to_eol(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("keep|drop this\nnext\n")
    supertool.op_vi(str(f), "/|;c$STOP")
    assert f.read_text() == "keepSTOP\nnext\n"


def test_c_zero_changes_to_bol(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("drop this|keep\n")
    supertool.op_vi(str(f), "/|;c0KEEP")
    assert f.read_text() == "KEEP|keep\n"


# ---------------------------------------------------------------------------
# Delete to motion: d$ d0 dw
# ---------------------------------------------------------------------------

def test_d_dollar_deletes_to_eol(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("keep|drop this\nnext\n")
    supertool.op_vi(str(f), "/|;d$")
    assert f.read_text() == "keep\nnext\n"


def test_d_zero_deletes_to_bol(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("drop|keep\n")
    supertool.op_vi(str(f), "/|;d0")
    assert f.read_text() == "|keep\n"


def test_dw_deletes_word_and_trailing_space(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("foo bar baz\n")
    supertool.op_vi(str(f), "dw")
    assert f.read_text() == "bar baz\n"


# ---------------------------------------------------------------------------
# Char-find motion: cf cF ct cT df dF dt dT yf yF yt yT
# ---------------------------------------------------------------------------

def test_cf_changes_through_target(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text('label = "old text"\n')
    supertool.op_vi(str(f), '/";l;cf"new text"')
    # Cursor at first ", `l` moves to `o`, cf" deletes up-to-and-incl next " → inserts "new text"
    assert f.read_text() == 'label = "new text"\n'


def test_ct_changes_up_to_target(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("foo, bar, baz\n")
    supertool.op_vi(str(f), "ct,XXX")
    assert f.read_text() == "XXX, bar, baz\n"


def test_df_deletes_through_target(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("aaXbbXcc\n")
    supertool.op_vi(str(f), "dfX")
    assert f.read_text() == "bbXcc\n"


def test_dt_deletes_up_to_target(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("aaXbb\n")
    supertool.op_vi(str(f), "dtX")
    assert f.read_text() == "Xbb\n"


# ---------------------------------------------------------------------------
# Yank & paste: yy yw y$ p P
# ---------------------------------------------------------------------------

def test_yy_then_p_duplicates_line(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("hello\nworld\n")
    supertool.op_vi(str(f), "yy;p")
    assert f.read_text() == "hello\nhello\nworld\n"


def test_yw_then_p_pastes_word(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("foo bar\n")
    supertool.op_vi(str(f), "yw;$;p")
    assert f.read_text() == "foo barfoo\n"


def test_y_dollar_then_P_pastes_before(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("abcdef\n")
    supertool.op_vi(str(f), "3l;y$;0;P")
    assert f.read_text() == "defabcdef\n"


# ---------------------------------------------------------------------------
# Ex substitute: :s/PAT/REPL/[gi]
# ---------------------------------------------------------------------------

def test_substitute_first_match(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("foo foo foo\n")
    supertool.op_vi(str(f), ":s/foo/BAR/")
    assert f.read_text() == "BAR foo foo\n"


def test_substitute_global(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("foo foo foo\n")
    supertool.op_vi(str(f), ":s/foo/BAR/g")
    assert f.read_text() == "BAR BAR BAR\n"


def test_substitute_case_insensitive(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("Foo FOO foo\n")
    supertool.op_vi(str(f), ":s/foo/x/gi")
    assert f.read_text() == "x x x\n"


def test_substitute_backref(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("a=1\nb=2\n")
    supertool.op_vi(str(f), ":s/(\\w+)=(\\d+)/\\2:\\1/g")
    assert f.read_text() == "1:a\n2:b\n"


def test_substitute_no_match_errors(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("hello\n")
    out = supertool.op_vi(str(f), ":s/missing/x/g")
    assert "ERROR" in out


def test_substitute_strips_defensive_backslash_before_punct(tmp_path: Path) -> None:
    """Kevin defensively writes `\\)\\;` in REPL — both must collapse to `);`."""
    f = tmp_path / "x.php"
    f.write_text("foo(1)\n")
    supertool.op_vi(str(f), ":s/\\)$/\\)\\;/")
    assert f.read_text() == "foo(1);\n"


def test_substitute_repl_with_php_namespace_backslashes(tmp_path: Path) -> None:
    """REPL containing single backslashes (PHP/JS namespace separators) must
    not crash re.sub with 'bad escape \\B' — backslashes are auto-escaped."""
    f = tmp_path / "x.php"
    f.write_text("use OLD;\n")
    supertool.op_vi(
        str(f),
        ":s/OLD/Shared\\BusinessEntities\\ReviewSessionHasEntity/",
    )
    assert f.read_text() == "use Shared\\BusinessEntities\\ReviewSessionHasEntity;\n"


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
