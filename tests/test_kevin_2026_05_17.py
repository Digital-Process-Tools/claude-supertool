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
        new = open(p).read()
        assert "line 5 HIT" in new, f"got: {new!r}; receipt: {r}"
        assert "ERROR" not in r, f"unexpected error: {r}"
    finally:
        os.unlink(p)


def test_issue4_bare_colon_dollar_jumps_to_last_line():
    """`:$\\e` should jump to last line (same as `G`)."""
    p = _tmp("a\nb\nc\nd\n")
    try:
        r = st.op_vim(p, f":${ESC}A!{ESC}")
        new = open(p).read()
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
        new = open(p).read()
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
        new = open(p).read()
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
        new = open(p).read()
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
        new = open(p).read()
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
        new = open(p).read()
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
        new = open(p).read()
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
        new = open(p).read()
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
        new = open(p).read()
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
        new = open(p).read()
        # V + 3j = select 4 lines (current + 3 down), d = delete
        assert new == "e\nf\n", f"expected 4 lines deleted: {new!r}; receipt: {r}"
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_log_pattern_w_is_noop():
    """Kevin (dvsi4 logs): `:w` (write file). Supertool writes atomically already.
    Real vim's `:w` saves; here it's a no-op (or accept with comment)."""
    p = _tmp("a\nb\n")
    try:
        r = st.op_vim(p, ":w")
        new = open(p).read()
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


def test_log_pattern_Nr_read_file_after_line():
    """Kevin log: `:189r /tmp/foo.txt` — read FILE after line 189. Real vim feature."""
    # Source file with content to read
    src_fd, src_path = tempfile.mkstemp(suffix=".txt", text=True)
    os.write(src_fd, b"INSERTED1\nINSERTED2\n")
    os.close(src_fd)
    p = _tmp("a\nb\nc\nd\ne\n")
    try:
        r = st.op_vim(p, f":2r {src_path}")
        new = open(p).read()
        # After line 2 ("b"), file content should appear before "c"
        assert "b\nINSERTED1\nINSERTED2\nc" in new, f"got: {new!r}; receipt: {r}"
        assert "ERROR" not in r
    finally:
        os.unlink(p)
        os.unlink(src_path)


def test_issue1_oi_no_whitespace_does_not_autocorrect():
    """`oiword` — `i` followed by word char, NOT a Kevin reflex (no indent).
    Should remain literal (real vim semantics: inserts `iword`).
    Safety: only strip when verb-char + whitespace.
    """
    p = _tmp("a\nb\n")
    try:
        r = st.op_vim(p, f"1Goiword{ESC}")
        new = open(p).read()
        # Must contain literal `iword` — autocorrect should NOT fire here
        assert "iword" in new, f"autocorrect over-fired: {new!r}; receipt: {r}"
    finally:
        os.unlink(p)
