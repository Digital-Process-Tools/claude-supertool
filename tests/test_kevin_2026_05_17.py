"""Failing tests for Kevin issues from real 2026-05-17 paste.

Source: /Users/floriandavid/Documents/dvsi/.max/kevin-issues-2026-05-17.md
"""
import os
import tempfile

import supertool as st


ESC = "\x1b"


def _tmp(content: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".php", text=True)
    os.write(fd, content.encode())
    os.close(fd)
    return path


# ---- Issue #4: bare `:N` line goto ----

def test_issue4_bare_colon_N_jumps_to_line():
    """Kevin: `111G35dd:110\\e:r FOO\\e`. `:110` should jump to line 110.
    Current behavior: 'unknown verb' error.
    """
    lines = [f"line {n}" for n in range(1, 21)]
    p = _tmp("\n".join(lines) + "\n")
    try:
        # Goto line 5 then append-EOL " HIT"
        r = st.op_vim(p, f":5{ESC}A HIT{ESC}")
        new = open(p, encoding="utf-8").read()
        assert "line 5 HIT" in new, f"got: {new!r}; receipt: {r}"
        assert "ERROR" not in r, f"unexpected error: {r}"
    finally:
        os.unlink(p)


def test_issue4_bare_colon_dollar_jumps_to_last_line():
    """`:$\\e` should jump to last line (same as `G`)."""
    p = _tmp("a\nb\nc\nd\n")
    try:
        r = st.op_vim(p, f":${ESC}A!{ESC}")
        new = open(p, encoding="utf-8").read()
        assert "d!" in new, f"got: {new!r}; receipt: {r}"
        assert "ERROR" not in r
    finally:
        os.unlink(p)


# ---- Issue #6 recurrence: `:%s/PAT(...)` unescaped paren ----

def test_issue6_subst_assertEquals_unescaped_paren():
    """Kevin: `:%s/assertEquals(/assertSame(/g`. Unescaped `(` — should literal-fallback."""
    p = _tmp("        $this->assertEquals(1, 2);\n        $this->assertEquals(3, 4);\n")
    try:
        r = st.op_vim(p, ":%s/assertEquals(/assertSame(/g")
        new = open(p, encoding="utf-8").read()
        assert "assertSame(1, 2)" in new, f"got: {new!r}; receipt: {r}"
        assert "assertSame(3, 4)" in new, f"got: {new!r}; receipt: {r}"
        assert "ERROR" not in r, f"unexpected error: {r}"
    finally:
        os.unlink(p)


# ---- Issue #1: verb-bleed — `oi<indent>TEXT` should not literally insert `i<indent>TEXT` ----

def test_issue1_oi_indent_text_strips_redundant_i():
    """Kevin: `78Goi        $this->assertX();\\e`.

    Real vim: `o` enters insert mode, so `i` after `o` literally inserts `i`.
    But Kevin's INTENT is always 'insert this text on new line'. The redundant
    insert-verb char followed by whitespace is a muscle-memory pattern from real
    vim users typing `o` then `i` reflexively. Strip the redundant verb.
    """
    lines = [f"line {n}" for n in range(1, 10)]
    p = _tmp("\n".join(lines) + "\n")
    try:
        r = st.op_vim(p, f"3Goi        new_line();{ESC}")
        new = open(p, encoding="utf-8").read()
        assert "\ni        new_line()" not in new, (
            f"`i` bled into insert (Kevin's muscle memory): {new!r}; receipt: {r}"
        )
        assert "        new_line();" in new, f"content missing: {new!r}; receipt: {r}"
    finally:
        os.unlink(p)


def test_issue7_double_backslash_paren_overescape():
    """Kevin T5: `:%s/assertEquals\\\\(/assertSame\\\\(/g`.
    `\\\\(` (single-quoted shell) reaches Python as `\\\\(` = literal `\\` + group-open.
    Should literal-fallback via over-escape strip.
    """
    p = _tmp("        $this->assertEquals(1, 2);\n")
    try:
        # In Python source `\\\\(` = 4 chars on disk = what Kevin's shell sent
        r = st.op_vim(p, r":%s/assertEquals\\(/assertSame\\(/g")
        new = open(p, encoding="utf-8").read()
        assert "assertSame(1, 2)" in new, f"got: {new!r}; receipt: {r}"
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_issue8_quad_backslash_namespace_insert():
    """Kevin T10: `ouse SiSearch\\\\SiSearchModule;\\e` — quad backslash in insert.

    Kevin wants `\\` (PHP namespace separator) in file. In single-quoted shell,
    he wrote `\\\\\\\\` (4 → file gets `\\\\`) WRONG, should be `\\\\` (2 → file gets `\\`).
    The insert path doesn't autocorrect today, so this just documents current behavior:
    `\\\\\\\\` arrives in arg as 4 backslashes; decoded into 2 backslashes in file.
    Acceptable IF the verb-bleed autocorrect or some heuristic handles it.
    For now: just verify it doesn't crash and produces SOMETHING parseable.
    """
    p = _tmp("use Foo;\n")
    try:
        # Kevin's actual single-quoted shell input → arg has 4 literal backslashes
        r = st.op_vim(p, "1Gouse SiSearch\\\\SiSearchModule;\x1b")
        new = open(p, encoding="utf-8").read()
        # Document current: insert lands literally (no autocorrect on insert yet)
        # If we later add autocorrect, change this test to expect single `\`
        assert "use SiSearch" in new, f"insert lost: {new!r}; receipt: {r}"
        assert "ERROR" not in r
    finally:
        os.unlink(p)


# ---- Patterns mined from jimmy-issue-142 jsonl logs (2026-05-17) ----

def test_log_pattern_double_percent_d_typo():
    """Kevin log: `:%%d` (typo, double %). Currently 'unknown verb'.
    Should autocorrect to `:%d` (delete whole buffer)."""
    p = _tmp("a\nb\nc\n")
    try:
        r = st.op_vim(p, ":%%d")
        new = open(p, encoding="utf-8").read()
        assert new == "" or new == "\n", f"buffer should be empty: {new!r}; receipt: {r}"
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_log_pattern_line_pattern_range_delete():
    """Kevin log: `:.,/^    }$/d` — delete from current line to next match of pattern.
    Real vim supports `:.,/PAT/d`."""
    p = _tmp("a\nb\nstart\nmid1\nmid2\n    }\nafter\n")
    try:
        r = st.op_vim(p, "/start\x1b:.,/^    }$/d")
        new = open(p, encoding="utf-8").read()
        # Lines from `start` through `    }` should be gone
        assert "start" not in new, f"start still present: {new!r}; receipt: {r}"
        assert "    }" not in new, f"}} still present: {new!r}; receipt: {r}"
        assert "after" in new, f"after gone: {new!r}; receipt: {r}"
    finally:
        os.unlink(p)


def test_log_pattern_relative_offset_range_works():
    """Regression: `:.,+1d` (delete current + next) should work.
    Verified working in source — lock it in."""
    p = _tmp("a\nb\nc\nd\ne\n")
    try:
        r = st.op_vim(p, "2G:.,+1d")
        new = open(p, encoding="utf-8").read()
        assert new == "a\nd\ne\n", f"got: {new!r}; receipt: {r}"
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_log_pattern_V_count_G_d():
    """Kevin log: `V145Gd` — visual line, extend to line 145, delete.
    Real vim: V (visual-line), 145G (goto line 145 extending selection), d (delete range).
    Equivalent to `:.,145d`."""
    lines = [f"line {n}" for n in range(1, 200)]
    p = _tmp("\n".join(lines) + "\n")
    try:
        r = st.op_vim(p, "5GV10Gd")
        new = open(p, encoding="utf-8").read()
        # Lines 5-10 (inclusive) should be gone
        assert "line 4\nline 11" in new, f"lines 5-10 should be deleted: {new[:100]!r}; receipt: {r}"
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_log_pattern_percent_on_non_bracket():
    """Kevin log: `'%': % not on a bracket char (found '<')`.
    Real vim: `%` on `<` jumps to matching `>` (for HTML/XML tags)."""
    p = _tmp("<div>content</div>\n")
    try:
        # Cursor at start (on `<`), `%` should jump to `>`
        r = st.op_vim(p, "%")
        # If supported, no ERROR. If not, current behavior is ERROR.
        # This documents the gap — change expectation when implemented.
        if "ERROR" not in r:
            assert True  # implemented
        else:
            # Currently broken — assert ERROR present (snapshot)
            assert "not on a bracket char" in r, f"expected bracket error: {r}"
    finally:
        os.unlink(p)


def test_log_pattern_V_motion_single_op():
    """Kevin (dvsi4 logs): `V3jd` — V + 3j motion + d (single op, not dd).
    Current `_V_MOTION_LINE` regex requires cc|dd|yy. Accept single op too."""
    p = _tmp("a\nb\nc\nd\ne\nf\n")
    try:
        r = st.op_vim(p, "V3jd")
        new = open(p, encoding="utf-8").read()
        # V + 3j = select 4 lines (current + 3 down), d = delete
        assert new == "e\nf\n", f"expected 4 lines deleted: {new!r}; receipt: {r}"
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_w_variants_all_noop():
    """:w / :wq / :wq! / :wa / :write / :x / :x! — all no-op (review fix #3)."""
    for cmd in (":w", ":wq", ":wq!", ":wa", ":write", ":x", ":x!"):
        p = _tmp("a\nb\n")
        try:
            r = st.op_vim(p, cmd)
            assert open(p, encoding="utf-8").read() == "a\nb\n", f"{cmd}: buffer modified: {r}"
            assert "ERROR" not in r, f"{cmd}: error: {r}"
        finally:
            os.unlink(p)


def test_goto_out_of_range_errors_cleanly():
    """`:999` on 3-line file — should give clear range error, not crash."""
    p = _tmp("a\nb\nc\n")
    try:
        r = st.op_vim(p, ":999")
        assert "ERROR" in r and ("out of range" in r or "line" in r.lower())
    finally:
        os.unlink(p)


def test_Nr_missing_file_errors_cleanly():
    """`:5r /nonexistent/file.txt` — clear error, no crash."""
    p = _tmp("a\nb\nc\n")
    try:
        r = st.op_vim(p, ":2r /nonexistent/xyz_kevin_test.txt")
        assert "ERROR" in r and ("failed to read" in r or "No such" in r)
        assert open(p, encoding="utf-8").read() == "a\nb\nc\n", "buffer should be unchanged"
    finally:
        os.unlink(p)


def test_line_pattern_range_no_match_errors_cleanly():
    """`:.,/NOMATCH/d` — pattern not found, clear error."""
    p = _tmp("a\nb\nc\n")
    try:
        r = st.op_vim(p, ":.,/zzz_nope/d")
        assert "ERROR" in r and "pattern not found" in r
        assert open(p, encoding="utf-8").read() == "a\nb\nc\n"
    finally:
        os.unlink(p)


def test_o_insert_auto_indent_matches_current_line():
    """Kevin (T4 2026-05-17 19:17): `o<TEXT>\\n        <TEXT2>\\e` — first line
    has no indent. Real vim's `autoindent` (default-ish) prepends current line's
    indent to first inserted char. Add it."""
    p = _tmp("class Foo\n{\n    function bar()\n    {\n        $x = 1;\n    }\n}\n")
    try:
        # Cursor at line 5 ("        $x = 1;"), `o` opens below at same indent
        r = st.op_vim(p, f"5Goself::doThing();{ESC}")
        new = open(p, encoding="utf-8").read()
        # Should be: line 5 stays, new line "        self::doThing();" follows
        assert "        $x = 1;\n        self::doThing();\n" in new, (
            f"auto-indent missing: {new!r}; receipt: {r}"
        )
    finally:
        os.unlink(p)


def test_O_insert_auto_indent_matches_current_line():
    """`O` opens above — same auto-indent rule."""
    p = _tmp("a\nb\n        $x = 1;\nc\n")
    try:
        r = st.op_vim(p, f"3GOself::doThing();{ESC}")
        new = open(p, encoding="utf-8").read()
        assert "b\n        self::doThing();\n        $x = 1;\n" in new, (
            f"auto-indent missing: {new!r}; receipt: {r}"
        )
    finally:
        os.unlink(p)


def test_o_insert_does_not_double_indent_when_text_starts_with_ws():
    """If TEXT explicitly starts with whitespace, don't add MORE indent."""
    p = _tmp("    indented_line\n")
    try:
        r = st.op_vim(p, f"1Go        explicit_indent{ESC}")
        new = open(p, encoding="utf-8").read()
        # First inserted char is space — Kevin already provided indent
        # Auto-indent should NOT prepend 4 more spaces on top of his 8
        assert new == "    indented_line\n        explicit_indent\n", (
            f"double indent: {new!r}; receipt: {r}"
        )
    finally:
        os.unlink(p)


def test_log_pattern_w_is_noop():
    """Kevin (dvsi4 logs): `:w` (write file). Supertool writes atomically already.
    Real vim's `:w` saves; here it's a no-op (or accept with comment)."""
    p = _tmp("a\nb\n")
    try:
        r = st.op_vim(p, ":w")
        new = open(p, encoding="utf-8").read()
        assert new == "a\nb\n", f"buffer should be unchanged: {new!r}"
        assert "ERROR" not in r, f"expected no error: {r}"
    finally:
        os.unlink(p)


def test_log_pattern_bare_V_keeps_hint():
    """Kevin log: bare `V` errors. Team decided ERROR-with-hint > silent no-op
    (test_unknown_verb_shows_suggestion_for_V locks this). Document the choice."""
    p = _tmp("a\nb\nc\n")
    try:
        r = st.op_vim(p, "V")
        assert "ERROR" in r and ("dd" in r or "cc" in r or "yy" in r), (
            f"expected actionable hint: {r}"
        )
    finally:
        os.unlink(p)


def test_log_pattern_Nr_read_file_after_line(tmp_path):
    """Kevin log: `:189r /tmp/foo.txt` — read FILE after line 189. Real vim feature.

    #1973: the source file used to come from a bare `tempfile.mkstemp()`,
    landing it in the shared platform temp root rather than under pytest's
    own per-test `tmp_path`. That is a mitigation against a race under
    xdist load, not a diagnosis of one -- see the issue for what remains
    unestablished."""
    src_path = str(tmp_path / "src.txt")
    with open(src_path, "wb") as f:
        f.write(b"INSERTED1\nINSERTED2\n")
    p = _tmp("a\nb\nc\nd\ne\n")
    try:
        r = st.op_vim(p, f":2r {src_path}")
        new = open(p, encoding="utf-8").read()
        # After line 2 ("b"), file content should appear before "c"
        assert "b\nINSERTED1\nINSERTED2\nc" in new, f"got: {new!r}; receipt: {r}"
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_issue12_bare_g_pattern_d_autocorrects_to_ex():
    """Kevin (T14 19:42): `g/assertIsArray/d` → 'unknown verb g'.
    Real vim needs `:g/PAT/d` (ex command). Muscle memory drops `:`.
    Autocorrect: bare `g/PAT/d` → `:g/PAT/d`."""
    p = _tmp("keep1\nremove this assertIsArray here\nkeep2\nassertIsArray again\nkeep3\n")
    try:
        r = st.op_vim(p, "g/assertIsArray/d")
        new = open(p, encoding="utf-8").read()
        assert "assertIsArray" not in new, f"lines should be gone: {new!r}; receipt: {r}"
        assert "keep1\nkeep2\nkeep3\n" == new, f"got: {new!r}"
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_issue12_bare_percent_g_pattern_d_autocorrects_to_ex():
    """Kevin (T18 19:42): `%g/assertIsArray/d` → '% not on bracket char'.
    `%g/PAT/d` should map to `:%g/PAT/d`."""
    p = _tmp("a\nfoo\nb\nfoo\nc\n")
    try:
        r = st.op_vim(p, "%g/foo/d")
        new = open(p, encoding="utf-8").read()
        assert "foo" not in new, f"got: {new!r}; receipt: {r}"
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_issue14_ex_append_Na_with_dot_terminator():
    """Kevin (T42 20:42): `:113a\\nTEXT\\nTEXT2\\n.\\e` — ex append after line N,
    body terminated by lone `.`. Real vim ex-mode feature."""
    p = _tmp("line1\nline2\nline3\nline4\n")
    try:
        # Append after line 2
        r = st.op_vim(p, ":2a\nAPPENDED1\nAPPENDED2\n.")
        new = open(p, encoding="utf-8").read()
        assert "line1\nline2\nAPPENDED1\nAPPENDED2\nline3\nline4\n" == new, (
            f"got: {new!r}; receipt: {r}"
        )
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_issue16_trailing_comma_after_digits_is_noop():
    """Kevin (jimmy log 18:39): `64,` action — `,` is find-repeat, errors with
    'no previous f/F/t/T'. Misleading. Autocorrect: strip abandoned range so
    Kevin gets a clean 'no actions' result rather than a wrong-context error."""
    p = _tmp("a\nb\nc\n")
    try:
        r = st.op_vim(p, "64,")
        assert open(p, encoding="utf-8").read() == "a\nb\nc\n", f"buffer modified: {r}"
        # Find-repeat error must NOT show — was the misleading bit.
        assert "f/F/t/T" not in r, f"misleading find-repeat error: {r}"
    finally:
        os.unlink(p)


def test_issue16_chained_abandoned_range_does_not_break_followups():
    """`64,\\edd` — Kevin abandoned the range, then a real action.
    Strip `64,` only, keep `dd` working."""
    p = _tmp("a\nb\nc\n")
    try:
        r = st.op_vim(p, "64,\x1bdd")
        assert open(p, encoding="utf-8").read() == "b\nc\n", f"dd should run: {open(p, encoding='utf-8').read()!r}; receipt: {r}"
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_paste_op_full_file_rewrite():
    """Kevin pain (Issue #11): full-file rewrite via vim macro cuts `<?php`.
    paste op = atomic full-file replace, no positioning math."""
    p = _tmp("<?php\n\n// OLD\nclass Old {}\n")
    try:
        new_content = "<?php\n\ndeclare(strict_types=1);\n\nclass Foo\n{\n    public function bar(): void {}\n}\n"
        r = st.op_paste(p, new_content)
        assert open(p, encoding="utf-8").read() == new_content, f"got: {open(p, encoding='utf-8').read()!r}; receipt: {r}"
        assert "<?php" in open(p, encoding="utf-8").read(), "<?php eaten!"
        assert "rewrote" in r
    finally:
        os.unlink(p)


def test_paste_op_creates_missing_file_and_parent():
    """paste creates file + parent dirs if missing — no `mkdir -p` round-trip."""
    d = tempfile.mkdtemp()
    p = os.path.join(d, "new", "nested", "file.txt")
    try:
        r = st.op_paste(p, "hello\nworld\n")
        assert open(p, encoding="utf-8").read() == "hello\nworld\n", f"got: {open(p, encoding='utf-8').read()!r}; receipt: {r}"
        assert "created" in r
    finally:
        import shutil
        shutil.rmtree(d)


def test_paste_op_appends_trailing_newline():
    """POSIX text files end in \\n. paste should append if missing."""
    p = _tmp("old\n")
    try:
        r = st.op_paste(p, "no trailing nl")
        assert open(p, encoding="utf-8").read() == "no trailing nl\n", f"got: {open(p, encoding='utf-8').read()!r}"
    finally:
        os.unlink(p)


def test_issue18_replace_lines_end_off_by_one_clamps_to_eof():
    """Kevin (CoverageAudit T8): `replace_lines:::PATH:::49:::57:::BODY` on 56-line file.
    Off-by-one — Kevin meant 'through EOF'. Clamp END to total + hint."""
    p = _tmp("\n".join(f"line{n}" for n in range(1, 57)) + "\n")  # 56 lines
    try:
        r = st.op_replace_lines(p, 49, 57, "REPLACED\n")
        new = open(p, encoding="utf-8").read()
        assert new.endswith("REPLACED\n"), f"got tail: {new[-50:]!r}; receipt: {r}"
        # Lines 49 onwards gone, replaced by REPLACED
        assert "line48\nREPLACED\n" == new[-len("line48\nREPLACED\n"):], (
            f"clamped result wrong: {new[-30:]!r}"
        )
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_issue18_replace_lines_end_two_over_still_errors():
    """END > total+1 = real mistake. Keep error."""
    p = _tmp("a\nb\nc\n")  # 3 lines
    try:
        r = st.op_replace_lines(p, 2, 5, "X\n")  # 5 > 3+1
        assert "ERROR" in r, f"should error: {r}"
        assert open(p, encoding="utf-8").read() == "a\nb\nc\n", "buffer should be unchanged"
    finally:
        os.unlink(p)


def test_issue17_o_question_pattern_autocorrects_to_search_then_open():
    """Kevin reflex (T6+T10 CoverageAudit): `o?PAT\\e<more>` — thinks `o?` searches
    backward then opens. Real vim inserts `?PAT` as literal. Autocorrect:
    detect `o<TEXT>\\e` where TEXT = `?<pattern>` short single-line → split
    into `?PAT\\eo<rest>\\e` (search-then-open)."""
    p = _tmp("class Foo\n{\n    function bar(): void\n    {\n        $x = 1;\n    }\n}\n")
    try:
        # Kevin's reflex: G + o?^}\eO<TEXT>\e — wanted: G, search ?^}, O TEXT
        r = st.op_vim(p, "Go?^}\x1bO    function baz(): void {}\x1b")
        new = open(p, encoding="utf-8").read()
        # After autocorrect: cursor goes to closing }, O inserts before it
        assert "    function baz(): void {}\n}" in new, f"insert misplaced: {new!r}; receipt: {r}"
        # `?^}` must NOT be a literal line in the file
        assert "?^}" not in new, "?^} leaked as text: " + repr(new)
    finally:
        os.unlink(p)


def test_issue17_o_slash_pattern_autocorrects():
    """Same pattern with `/PAT` forward search."""
    p = _tmp("alpha\nbeta\nGAMMA\ndelta\n")
    try:
        r = st.op_vim(p, "ggo/GAMMA\x1boFOUND\x1b")
        new = open(p, encoding="utf-8").read()
        # `?GAMMA` → cursor on GAMMA line, o opens below
        assert "GAMMA\nFOUND\n" in new, f"got: {new!r}; receipt: {r}"
        assert "/GAMMA" not in new, f"/GAMMA leaked: {new!r}"
    finally:
        os.unlink(p)


def test_issue17_o_question_with_spaces_does_not_autocorrect():
    """If TEXT has whitespace, Kevin likely meant content. Don't autocorrect."""
    p = _tmp("line1\nline2\n")
    try:
        r = st.op_vim(p, "1Go? what is this content\x1b")
        new = open(p, encoding="utf-8").read()
        # Should insert as literal — has space, not a search pattern
        assert "? what is this content" in new, f"autocorrect over-fired: {new!r}"
    finally:
        os.unlink(p)


def test_issue1_oi_no_whitespace_does_not_autocorrect():
    """`oiword` — `i` followed by word char, NOT a Kevin reflex (no indent).
    Should remain literal (real vim semantics: inserts `iword`).
    Safety: only strip when verb-char + whitespace.
    """
    p = _tmp("a\nb\n")
    try:
        r = st.op_vim(p, f"1Goiword{ESC}")
        new = open(p, encoding="utf-8").read()
        # Must contain literal `iword` — autocorrect should NOT fire here
        assert "iword" in new, f"autocorrect over-fired: {new!r}; receipt: {r}"
    finally:
        os.unlink(p)
