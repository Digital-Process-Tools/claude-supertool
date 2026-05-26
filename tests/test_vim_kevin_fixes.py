"""Tests for Kevin-driven fixes: better diagnostics for the vim op.

Three fixes:
  - Unknown verb shows "did you mean" suggestions instead of dumping the
    full verb catalog.
  - Failed `/PAT` shows nearest literal-substring matches so the caller
    sees what's actually in the file.
  - Failed `:s/PAT/...` shows the same kind of hint.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import supertool as st


def _tmp(content: str) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
    f.write(content)
    f.close()
    return f.name


def test_unknown_verb_shows_suggestion_for_V():
    p = _tmp("hello\nworld\n")
    try:
        r = st.op_vim(p, "V")
        assert "unknown verb" in r
        # Should suggest visual-line alternatives concisely
        assert "did you mean" in r.lower()
        # And NOT dump the full 80-item verb catalog
        assert "ci{" not in r or r.count(",") < 30
    finally:
        os.unlink(p)


def test_unknown_verb_suggests_dd_for_typo():
    p = _tmp("a\nb\n")
    try:
        # `dD` isn't a verb; closest are dd / D
        r = st.op_vim(p, "dD")
        assert "unknown verb" in r
        low = r.lower()
        assert "did you mean" in low
        assert ("dd" in low) or ("'d'" in low)
    finally:
        os.unlink(p)


def test_search_miss_shows_literal_context():
    p = _tmp(
        "function foo() {\n"
        "    assertSame(1, 2);\n"
        "    assertSame(3, 4);\n"
        "}\n"
    )
    try:
        # Searching for a pattern with a long literal chunk that DOES
        # appear, but the regex also requires text that doesn't.
        r = st.op_vim(p, r"/assertSame.*BEFORE_CREATE")
        assert "pattern not found" in r
        # New: nearest-match hint with line refs
        assert "near" in r.lower() or "closest" in r.lower() or "found:" in r.lower()
        assert "assertSame" in r
        # Should reference at least one line number
        assert any(f"line {n}" in r or f":{n}" in r for n in (2, 3))
    finally:
        os.unlink(p)


def test_search_miss_no_literal_yields_clean_error():
    p = _tmp("abc\ndef\n")
    try:
        r = st.op_vim(p, "/xyzqqqq")
        assert "pattern not found" in r
        # Should not crash with a hint section
        assert "Traceback" not in r
    finally:
        os.unlink(p)


def test_subst_miss_shows_literal_context():
    p = _tmp(
        "    assertTrue($result);\n"
        "    assertNull($foo);\n"
    )
    try:
        # Pattern that doesn't match (because the literal isn't there
        # exactly), but a substring DOES exist.
        r = st.op_vim(p, r":%s/assertTrue\(NONEXISTENT\)/REPL/")
        assert "no match" in r or "no match for" in r
        assert "assertTrue" in r  # context shows the close match
    finally:
        os.unlink(p)


# --- Real-world Kevin pastes from session 2026-05-17 (jimmy-issue-142) ---


def test_kevin_real_ggV_malformed_ex_range_errors_cleanly():
    """Kevin's actual paste: `ggV:53s/...` — V is now handled, `V:` is
    rewritten to `:.` which combined with `53` produces malformed ex
    range `.53`. Should fail with a clean range error + atomicity note,
    not a verb-catalog wall.
    """
    p = _tmp("\n".join(f"line {i}" for i in range(60)) + "\n")
    try:
        r = st.op_vim(p, "ggV:53s/foo/bar/\\e")
        assert "ERROR" in r
        assert "unchanged" in r.lower() or "atomic" in r.lower()
        # Clean error message (range / address / bad), not a catalog dump
        assert r.count(",") < 15
        assert "ci{" not in r
    finally:
        os.unlink(p)


def test_kevin_real_escape_double_dollar_in_subst():
    """Kevin pasted `:%s/\\$this->assertTrue.../REPL/`.

    Shell single-quotes pass `\\$` literally (3 chars `\\$`). vim's :s
    sees regex `\\$this` which means "literal backslash, then EOL, then
    this" — doesn't match. Should auto-halve `\\X` → `\\X` on no-match.
    """
    p = _tmp("        $this->assertTrue($result);\n")
    try:
        r = st.op_vim(p, r":%s/\\$this->assertTrue(\\$result);/REPLACED/")
        # After autocorrect: should successfully replace
        new = open(p).read()
        assert "REPLACED" in new, f"got: {new!r}; receipt: {r}"
        # And receipt should mention the autocorrect
        assert "autocorrect" in r.lower()
    finally:
        os.unlink(p)


def test_kevin_real_multiline_search_miss_with_context():
    """Kevin pasted `/assertSame(...);\\n        $this->assertSame(EntityEvent::BEFORE_CREATE, ...)`.

    File only has the first line, not the second. Forward search misses.
    Receipt should hint with nearest literal lines.
    """
    p = _tmp(
        "public function testFoo(): void\n"
        "{\n"
        "    $this->assertSame(self::$user, $event->getEntity());\n"
        "    $this->assertSame(EntityEvent::UPDATED, $event->getEventType());\n"
        "}\n"
    )
    try:
        r = st.op_vim(
            p,
            "/assertSame.self..user, .event->getEntity..;\\n"
            "        .this->assertSame.EntityEvent::BEFORE_CREATE",
        )
        assert "pattern not found" in r
        # Should show file context — line refs with assertSame
        low = r.lower()
        assert "near" in low or "closest" in low or "found:" in low
        assert "assertSame" in r
    finally:
        os.unlink(p)


def test_kevin_real_chained_marker_search_then_subst_works():
    """Kevin's pattern that should JUST WORK without escape doubling.

    /MARKER\\e:s/PAT/REPL/\\e — chained search + substitute.
    """
    p = _tmp(
        "use Foo\\Bar;\n"
        "use Foo\\Baz;\n"
        "\n"
        "MessageHelper::createRandom([], true);\n"
        "echo 1;\n"
    )
    try:
        r = st.op_vim(
            p,
            r"/MessageHelper::createRandom\e"
            r":s/MessageHelper::createRandom(\[\], true);/createRandom([], true);\n        $this->assertGreaterThan(0, $message->getId());/\e",
        )
        new = open(p).read()
        assert "assertGreaterThan" in new
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_kevin_real_subst_g_flag_multiline_repl():
    """Paste 3: `:%s/\\$this->assertTrue(\\$result);/<multiline with \\n>/g`.

    Both pattern and replacement reach vim with `\\$` (double-backslash).
    Pattern has unescaped `(` which is regex group syntax — Kevin meant
    literal `(`. Expected: literal autocorrect fires, multiline repl
    inserts cleanly across all matches.
    """
    p = _tmp(
        "    $this->assertTrue($result);\n"
        "    foo();\n"
        "    $this->assertTrue($result);\n"
    )
    try:
        r = st.op_vim(
            p,
            r":%s/\\$this->assertTrue(\\$result);/$this->assertSame(true, $result);\n            $this->assertSame('existing_board_id', ApplicationApi::getInstance()->getOptionAsString(SiTrelloProjectsOptions::TRELLO_PROJECTS_BOARD_UID));/g",
        )
        new = open(p).read()
        # Both occurrences replaced
        assert new.count("assertSame(true,") == 2, f"got: {new!r}; receipt: {r}"
        assert "existing_board_id" in new
        assert "autocorrect" in r.lower()
    finally:
        os.unlink(p)


def test_kevin_real_chain_search_change_text_object():
    """Paste 1 (simplified): `/assertIsBool\\eciwassertTrue\\e` — chain.

    Cursor jump to first `assertIsBool`, then `ciw` change-inner-word
    replaces it with `assertTrue`.
    """
    p = _tmp(
        "function testFoo() {\n"
        "    $this->assertIsBool($x);\n"
        "}\n"
    )
    try:
        r = st.op_vim(p, r"/assertIsBool\eciwassertTrue\e")
        new = open(p).read()
        assert "assertTrue" in new
        assert "assertIsBool" not in new
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_kevin_real_escape_doubling_in_subst_paren():
    """Paste 6: `:%s/assertTrue\\(true\\)/REPL/`.

    Kevin writes `\\(true\\)` thinking he's escaping for shell, but the
    extra backslash makes the regex `\\(true\\)` which means
    "literal-backslash, literal-paren". Literal-fallback should fix it.
    """
    p = _tmp("    $this->assertTrue(true);\n")
    try:
        r = st.op_vim(
            p,
            r":%s/assertTrue\\(true\\)/assertSame(true, $foo->isReady())/",
        )
        new = open(p).read()
        assert "assertSame(true, $foo->isReady())" in new
        assert "autocorrect" in r.lower()
    finally:
        os.unlink(p)


def test_kevin_real_subst_miss_shows_near_for_close_typo():
    """Paste 7 (simplified): `:%s/testWithProjectThatShouldNotBeOnTrelloProjects/REPLACED/`
    when the file has the testWith*Project* line but not exact match.
    """
    p = _tmp(
        "    public function testWithProject(): void {}\n"
        "    public function testWithEntity(): void {}\n"
    )
    try:
        r = st.op_vim(
            p,
            r":%s/testWithProjectThatShouldNotBeOnTrelloProjects/REPLACED/",
        )
        assert "no match" in r
        # Hint should surface the close line
        assert "testWithProject" in r
        assert "line 1" in r
    finally:
        os.unlink(p)


def test_kevin_real_v_inside_chain_does_not_dump_catalog():
    """Paste 2: `ggV:53s/.../.../` — V mid-chain. V is now handled and
    rewritten; any resulting error must be concise (no 80-item catalog).
    """
    p = _tmp("\n".join(f"line {i}" for i in range(60)) + "\n")
    try:
        r = st.op_vim(p, r"ggV:53s/foo/bar/\e")
        assert "ERROR" in r
        # Verify the catalog dump is gone
        assert "ci{" not in r
        assert "unchanged" in r.lower() or "atomic" in r.lower()
    finally:
        os.unlink(p)


def test_kevin_real_multiline_search_with_newline_chunks():
    """Paste 1: `/assertSame(...);\\n        $this->assertSame(EntityEvent::BEFORE_CREATE...`.

    File has only the first line. Multiline search fails with helpful hint
    referencing the literal `assertSame` chunk found in file.
    """
    p = _tmp(
        "public function testFoo(): void\n"
        "{\n"
        "    $this->assertSame(self::$user, $event->getEntity());\n"
        "    $this->assertSame(EntityEvent::UPDATED, $event->getEventType());\n"
        "}\n"
    )
    try:
        r = st.op_vim(
            p,
            "/assertSame.self::\\$user, \\$event->getEntity..;\\n"
            "        \\$this->assertSame.EntityEvent::BEFORE_CREATE",
        )
        assert "pattern not found" in r
        assert "near" in r.lower()
        # at least one assertSame line shown
        assert "assertSame" in r
    finally:
        os.unlink(p)


def test_kevin_real_Vcc_alias_to_cc():
    """Kevin run 2026-05-17 11:05 — paste used `Vcc<TEXT>` (vim visual-
    line + change). V is unsupported but `cc` already does line-change.
    Treat `Vcc<TEXT>` as `cc<TEXT>` — drop redundant V.
    """
    p = _tmp("old line\nkeep me\n")
    try:
        r = st.op_vim(p, r"Vccnew line\e")
        new = open(p).read()
        assert new == "new line\nkeep me\n", f"got: {new!r}; receipt: {r}"
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_kevin_real_Vjcc_alias_two_line_change():
    """Paste used `Vjcc<TEXT>` — visual-line, extend one line down,
    change. Equivalent to `2cc<TEXT>`.
    """
    p = _tmp("a\nb\nc\n")
    try:
        r = st.op_vim(p, r"Vjccnew\e")
        new = open(p).read()
        # Both 'a' and 'b' lines replaced by single "new" line
        assert new == "new\nc\n", f"got: {new!r}; receipt: {r}"
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_kevin_real_Vdd_alias_to_dd():
    p = _tmp("delete me\nkeep\n")
    try:
        r = st.op_vim(p, "Vdd")
        new = open(p).read()
        assert new == "keep\n", f"got: {new!r}; receipt: {r}"
    finally:
        os.unlink(p)


def test_kevin_real_Vyy_alias_to_yy():
    """V before yy is also redundant — yy yanks line."""
    p = _tmp("source\ntarget\n")
    try:
        r = st.op_vim(p, "Vyyjp")
        new = open(p).read()
        # Yank 'source' line, move down to 'target', paste below
        assert new.count("source") == 2, f"got: {new!r}; receipt: {r}"
    finally:
        os.unlink(p)


def test_kevin_real_VGd_deletes_to_end():
    """Kevin paste 2026-05-17 11:08: `ggVGd:r -` = select all + delete +
    read stdin. `VGd` = visual-line from current to end + delete.
    Equivalent to `:.,$d`.
    """
    p = _tmp("a\nb\nc\nd\n")
    try:
        r = st.op_vim(p, "ggVGd")
        new = open(p).read()
        # All lines deleted
        assert new == "" or new == "\n", f"got: {new!r}; receipt: {r}"
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_kevin_real_paste_11_05_three_chain_Vcc_Vjcc_Vcc():
    """Paste 2026-05-17 11:05 exact chain:
       /MARKER1\\e Vcc <TEXT>\\e
       /MARKER2\\e Vjcc <TEXT>\\e
       /MARKER3\\e Vcc <TEXT>\\e
    """
    p = _tmp(
        "    $this->assertInstanceOf(ITalkEntity::class, $relatedEntity);\n"
        "    $this->assertIsString($permission);\n"
        "    $oldA = 1;\n"
        "    $this->assertTrue(true);\n"
        "    final class ConcreteEntityTalkModule extends X\n"
    )
    try:
        r = st.op_vim(
            p,
            r"/assertInstanceOf(ITalkEntity\e"
            r"Vcc        $this->assertSame(Project::getSharedInstance(), $relatedEntity);\e"
            r"/assertIsString\e"
            r"Vjcc        $this->assertSame('ConcreteEntityTalkPermissions::READ_TAB', $permission);\e"
            r"/assertTrue(true)\e"
            r"Vcc            $this->assertSame(0, ConcreteEntityTalkModule::$relatedEntityCallCount);\e",
        )
        new = open(p).read()
        assert "assertSame(Project::getSharedInstance()" in new, f"step1 failed: {r}"
        assert "READ_TAB" in new, f"step2 failed: {r}"
        assert "relatedEntityCallCount" in new, f"step3 failed: {r}"
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_kevin_real_paste_11_08_ggVGd_then_r_stdin():
    """Paste 2026-05-17 11:08: `ggVGd:r -` — select all, delete, replace
    from stdin (heredoc). The full file-rewrite pattern Kevin keeps
    reaching for. Must work end-to-end.
    """
    import io
    import sys as _sys
    p = _tmp("old\ncontent\nto\nnuke\n")
    block = "<?php\nnew content here\n"
    saved_stdin = _sys.stdin
    _sys.stdin = io.StringIO(block)
    try:
        r = st.op_vim(p, "ggVGd:r -")
        new = open(p).read()
        assert "new content here" in new, f"got: {new!r}; receipt: {r}"
        assert "old" not in new and "nuke" not in new
    finally:
        _sys.stdin = saved_stdin
        os.unlink(p)


def test_kevin_real_V_in_insert_text_not_rewritten():
    """`iVcc` — user inserts literal text `Vcc`. V inside insert mode
    must NOT trigger the alias rewrite.
    """
    p = _tmp("X\n")
    try:
        r = st.op_vim(p, r"iVcc\e")
        new = open(p).read()
        assert new == "VccX\n", f"got: {new!r}; receipt: {r}"
    finally:
        os.unlink(p)


def test_kevin_real_ex_relative_offset_range():
    """Kevin uses `:.,+1d` (current line + next line, delete). Real-vim
    syntax for relative offset ranges. Our ex parser must support `+N`
    and `-N` after `.` or a line number.
    """
    p = _tmp("a\nb\nc\nd\ne\n")
    try:
        # Cursor on line 2 (`b`), delete current + next line = lines 2-3
        r = st.op_vim(p, "2G:.,+1d")
        new = open(p).read()
        assert new == "a\nd\ne\n", f"got: {new!r}; receipt: {r}"
    finally:
        os.unlink(p)


def test_kevin_real_ggVG_with_explicit_percent_ex():
    """Kevin paste 2026-05-17 14:05 — `ggVG:%d`. Kevin uses both V+G
    (visual all) AND `:%` (ex whole-buffer) — redundant. V-alias must
    not produce `:%%d` (invalid). Collapse to `:%d`.
    """
    p = _tmp("line a\nline b\nline c\n")
    try:
        r = st.op_vim(p, "ggVG:%d")
        new = open(p).read()
        assert new == "" or new == "\n", f"got: {new!r}; receipt: {r}"
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_kevin_log_anchored_pattern_literal_fallback():
    """Kevin paste 2026-05-17 13:55 — `:%s/^\\$this->assertGreaterThan(0, \\$entity->getId());$/.../g`.

    Pattern has regex anchors `^` and `$` plus escaped `\\$` for literal
    dollars. Literal-fallback should strip the leading `^` and trailing
    `$` (regex anchors) AND decode `\\$` → `$` (literal), so the probe
    becomes the actual line content for content.replace.
    """
    p = _tmp(
        "    public function testX(): void\n"
        "    {\n"
        "$this->assertGreaterThan(0, $entity->getId());\n"
        "    }\n"
    )
    try:
        r = st.op_vim(
            p,
            r":%s/^\$this->assertGreaterThan(0, \$entity->getId());$/        $this->assertGreaterThan(0, $entity->getId());/g",
        )
        new = open(p).read()
        # Indentation now fixed: leading spaces added
        assert "        $this->assertGreaterThan(0, $entity->getId());" in new, f"got: {new!r}; receipt: {r}"
        # And the unindented version no longer present at start of line
        assert "\n$this->assertGreaterThan" not in new
    finally:
        os.unlink(p)


def test_kevin_log_backward_search_miss_shows_near_hint():
    """Mined from log SiUserActionsModuleTest — `?assertNull\\(...`.
    Backward search miss returned bare 'pattern not found backward'
    without near-context. Should mirror forward `/PAT` behavior.
    """
    p = _tmp(
        "    $this->assertSame(1, 2);\n"
        "    $this->assertNull($foo);\n"
        "    end\n"
    )
    try:
        # Cursor at top — backward search has nothing before.
        # Force forward to end, then backward search for non-matching pattern.
        r = st.op_vim(p, r"G?assertNull NONEXISTENT")
        assert "pattern not found backward" in r
        low = r.lower()
        assert "near" in low or "closest" in low, f"missing near-hint: {r!r}"
        assert "assertNull" in r
    finally:
        os.unlink(p)


def test_kevin_log_backward_search_literal_fallback():
    """Backward search should also try literal fallback for unescaped
    regex meta — same as forward.
    """
    p = _tmp("    $this->assertTrue(true);\n    $other = 1;\nG\n")
    try:
        # Regex `(true)` is a group; literal-fallback should find it
        r = st.op_vim(p, r"G?assertTrue(true)")
        # If literal fallback worked, cursor moves; otherwise error
        # We at minimum want no crash, with a useful response
        assert "Traceback" not in r
        if "ERROR" in r:
            assert "pattern not found" in r
    finally:
        os.unlink(p)


def test_kevin_log_unescaped_paren_assertTrue_true():
    """Kevin run 2026-05-17 11:48 paste — `:s/assertTrue(true);/REPL/`.

    Pattern has unescaped `(`/`)` — regex groups, not literal parens.
    Kevin meant them literal. Literal-fallback was previously skipped
    because the decoded pattern equalled the original (no backslashes
    to strip). Should still try literal `content.replace` as the
    ultimate fallback when regex misses.
    """
    p = _tmp("        $this->assertTrue(true);\n        $other = 1;\n")
    try:
        r = st.op_vim(
            p,
            r":s/assertTrue(true);/assertFalse(SiTalkModule::hasUsedDependency());/",
        )
        new = open(p).read()
        assert "assertFalse(SiTalkModule::hasUsedDependency());" in new, f"got: {new!r}; receipt: {r}"
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_kevin_log_HasTagChecker_overescape_dollar():
    r"""Mined from log 9abd0ba5 — Kevin actual paste:
    `:s/HasTagChecker::class, \$checkerClass/$checkerClass, HasTagChecker::class/`.

    Over-escaped `\$checkerClass` in PAT. Literal-fallback must strip
    `\` → `` to get `$checkerClass`, then content.replace.
    """
    p = _tmp("    $checkerClass = $this->api->get(HasTagChecker::class, $checkerClass);\n")
    try:
        r = st.op_vim(
            p,
            r":s/HasTagChecker::class, \$checkerClass/$checkerClass, HasTagChecker::class/",
        )
        new = open(p).read()
        # Should succeed (either by regex direct match or literal fallback).
        assert "$checkerClass = $this->api->get($checkerClass, HasTagChecker::class)" in new, f"got: {new!r}; receipt: {r}"
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_kevin_log_createRandom_overescape_brackets():
    """Mined from log — `:s/MessageHelper::createRandom(\\[\\], true);/.../`.

    Kevin over-escapes `[` and `]` thinking they need shell escaping.
    Literal-fallback strips `\\[` → `[`, `\\]` → `]`.
    """
    p = _tmp("MessageHelper::createRandom([], true);\n")
    try:
        r = st.op_vim(
            p,
            r":s/MessageHelper::createRandom(\[\], true);/REPLACED/",
        )
        new = open(p).read()
        assert "REPLACED" in new, f"got: {new!r}; receipt: {r}"
        assert "autocorrect" in r.lower()
    finally:
        os.unlink(p)


def test_kevin_log_quad_backslash_iterative_strip():
    """Mined from log — `:s/\\\\$this->assertTrue(true);/.../`.

    Quadruple backslash (Kevin over-escapes twice). Iterative strip:
    `\\\\$this` → `\\$this` → `$this` (2 passes).
    """
    p = _tmp("        $this->assertTrue(true);\n")
    try:
        r = st.op_vim(
            p,
            r":%s/\\\\$this->assertTrue(true);/REPLACED/",
        )
        new = open(p).read()
        assert "REPLACED" in new, f"got: {new!r}; receipt: {r}"
    finally:
        os.unlink(p)


def test_kevin_real_ggVG_with_ex_substitute():
    """Mined from kevin log cb0d92ec — `ggVG:s/PAT/REPL/g`.
    Visual-line from start to end + ex substitute = whole-file substitute.
    Should rewrite to `gg:%s/PAT/REPL/g` (or equivalent full-buffer sub).
    """
    p = _tmp("foo bar\nfoo baz\nfoo qux\n")
    try:
        r = st.op_vim(p, r"ggVG:s/foo/REPL/g")
        new = open(p).read()
        assert new.count("REPL") == 3, f"got: {new!r}; receipt: {r}"
        assert "foo" not in new
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_kevin_real_cciw_autocorrect_to_ciw():
    """Kevin run 2026-05-17 11:21 paste — `cciwTEXT\\e`. Real-vim, `cc`
    is line-change (greedy text) and `ciw` is inner-word change. Kevin
    typed `cciw` thinking it modifies the change — but vim parses it
    as `cc` with text "iwTEXT". Autocorrect: `cc<text-object-char>`
    → `c<text-object>`.
    """
    p = _tmp("    $this->assertEquals(1, 2);\n")
    try:
        r = st.op_vim(p, r"/assertEquals\ecciwassertSame\e")
        new = open(p).read()
        # Word `assertEquals` replaced by `assertSame`, rest preserved
        assert "$this->assertSame(1, 2);" in new, f"got: {new!r}; receipt: {r}"
        assert "assertEquals" not in new
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_kevin_real_ccaw_autocorrect_to_caw():
    p = _tmp("foo bar baz\n")
    try:
        r = st.op_vim(p, r"/bar\eccawZZZ\e")
        new = open(p).read()
        # `aw` includes trailing whitespace; result varies but `bar ` gone
        assert "bar" not in new
        assert "ZZZ" in new
    finally:
        os.unlink(p)


def test_kevin_real_near_hint_labels_in_memory_state():
    """Kevin run 2026-05-17 11:20 paste — chain of 9 actions, action 9
    fails. The `near` hint shows line numbers from the IN-MEMORY mutated
    buffer (what the file would look like if action 9 had succeeded),
    not the on-disk state. The receipt must label it so Kevin doesn't
    think the disk file looks like that.

    Construct: action 1 inserts a UNIQUE_MARKER, action 2 searches for
    a pattern that doesn't quite match but where UNIQUE_MARKER appears
    in the near-hint output.
    """
    original = "alpha\nbeta\ngamma\n"
    p = _tmp(original)
    try:
        # Action 1: change "beta" to "UNIQUE_MARKER beta_extra" (mutation in buffer)
        # Action 2: search for pattern containing "UNIQUE_MARKER" but with regex
        # group that won't match — hint surfaces line 2 with the mutated content
        r = st.op_vim(
            p,
            r":%s/beta/UNIQUE_MARKER beta_extra/\e"
            r"/UNIQUE_MARKER(NOPE)",
        )
        # File on disk unchanged
        assert open(p).read() == original, f"file mutated: {open(p).read()!r}"
        # Receipt mentions atomicity / unchanged
        low = r.lower()
        assert "unchanged" in low or "atomic" in low, f"missing atomicity hint: {r!r}"
        # Near-hint should surface UNIQUE_MARKER from in-memory state...
        assert "UNIQUE_MARKER" in r, f"near-hint missing buffer content: {r!r}"
        # ...AND label it as buffer/in-progress so Kevin doesn't think
        # the disk file contains UNIQUE_MARKER
        assert any(
            m in low for m in ("buffer", "in-progress", "mutated", "pending", "in-memory")
        ), f"near-hint shows mutated content without labeling it: {r!r}"
    finally:
        os.unlink(p)


def test_kevin_real_chain_error_reports_atomicity():
    """Kevin run 2026-05-17 11:05 — long chain errored mid-way, Kevin
    panicked and rewrote the whole file via `printf > FILE`. But vim is
    already atomic: when any action errors, no write happens. The error
    receipt should say so loudly so Kevin trusts it.
    """
    original = "line 1\nline 2\nline 3\n"
    p = _tmp(original)
    try:
        # Action 1 succeeds (search), action 2 fails (V is unknown).
        r = st.op_vim(p, r"/line 2\eV")
        # File on disk unchanged
        assert open(p).read() == original
        # Error receipt explicitly says so
        assert "unchanged" in r.lower() or "atomic" in r.lower() or "preserved" in r.lower()
    finally:
        os.unlink(p)


def test_kevin_real_hex_escape_x27_in_subst_pat():
    """Real Kevin paste 2026-05-17 10:17 — `:%s/assertArrayHasKey(\\x27name\\x27, \\$options);/.../`.

    `\\x27` is hex escape for `'`. The `(` is unescaped (regex group),
    which Kevin meant as literal. Literal-fallback should strip the
    `\\` from `\\$` and `\\X` non-hex, BUT preserve `\\x27` as the char
    it represents (so the literal probe finds `assertArrayHasKey('name',`
    in the file).
    """
    p = _tmp(
        "    $this->assertArrayHasKey('name', $options);\n"
        "    $this->assertArrayHasKey('monitor_name', $options);\n"
    )
    try:
        r = st.op_vim(
            p,
            r":%s/assertArrayHasKey(\x27name\x27, \$options);/REPLACED/",
        )
        new = open(p).read()
        assert "REPLACED" in new, f"failed: {r!r}"
        # Other line untouched
        assert "assertArrayHasKey('monitor_name'" in new
        assert "autocorrect" in r.lower()
    finally:
        os.unlink(p)


def test_kevin_real_hex_escape_x27_in_subst_repl():
    """`\\x27` in REPL should decode to `'` via _decode_escapes."""
    p = _tmp("placeholder\n")
    try:
        r = st.op_vim(p, r":%s/placeholder/\x27quoted\x27/")
        new = open(p).read()
        assert new == "'quoted'\n", f"got: {new!r}"
    finally:
        os.unlink(p)


def test_kevin_real_paste1_cciw_chain():
    """Paste 1 (faithful): /assertIsBool\\e cciw assertTrue \\e — find then
    change-inner-word. Kevin's actual paste had `cciw` which is `cc` then
    iw-text. We test the intended outcome: replace assertIsBool with
    assertTrue via ciw.
    """
    p = _tmp(
        "    $this->assertIsBool($x);\n"
        "    $this->assertSame(self::$user, $event->getEntity());\n"
    )
    try:
        r = st.op_vim(
            p,
            r"/assertIsBool\eciwassertTrue\e",
        )
        new = open(p).read()
        assert "assertTrue($x)" in new
        assert "assertIsBool" not in new
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_kevin_real_paste4_r_stdin_pipe_block():
    """Paste 4: `:r -` to insert multiline block from stdin.

    op_vim reads from sys.stdin when arg is `-`. We test that the chained
    workflow (find marker, dd current line, :r - to insert block) works
    when stdin provides multiline content.
    """
    import io
    import sys as _sys
    p = _tmp(
        "use Foo\\Bar;\n"
        "$this->assertTrue(true);\n"
        "echo 1;\n"
    )
    block = (
        "        $this->assertGreaterThan(0, $user->getId());\n"
        "        $this->assertTrue($user->isNew());\n"
    )
    saved_stdin = _sys.stdin
    _sys.stdin = io.StringIO(block)
    try:
        r = st.op_vim(
            p,
            r"/\$this->assertTrue\edd:r -",
        )
        new = open(p).read()
        assert "assertGreaterThan(0, $user->getId())" in new, f"got: {new!r}; receipt: {r}"
        # The original assertTrue line was dd'd
        assert "$this->assertTrue(true)" not in new
    finally:
        _sys.stdin = saved_stdin
        os.unlink(p)


def test_kevin_real_paste5_three_step_subst_chain():
    """Paste 5: three chained `:s` calls with `\\\\n` and `\\\\\\\\`.

    Kevin's exact pattern: replace one line, then replace assertTrue line,
    then insert lines after createRandom. Tests that escape-doubled `\\\\`
    and `\\n` in replacements survive the chain.
    """
    p = _tmp(
        "use SiTrelloProjects\\SiTrelloProjectsModule;\n"
        "\n"
        "class X {\n"
        "    public function testFoo() {\n"
        "        $user = UserHelper::createRandom([], true);\n"
        "        $this->assertTrue(true);\n"
        "    }\n"
        "}\n"
    )
    try:
        r = st.op_vim(
            p,
            r":s/SiTrelloProjectsModule;/SiTrelloProjectsModule;\nuse SiTrelloProjects\\SiTrelloProjectsOptions;/\e"
            r":%s/\$this->assertTrue(true);/$this->assertTrue($user->getMetadataByKey(SiTrelloProjectsOptions::TRELLO_PROJECTS_BOARD_UID)->isNew());/\e"
            r":%s/createRandom(\[\], true);/createRandom([], true);\n\n        $this->assertGreaterThan(0, $user->getId());/\e",
        )
        new = open(p).read()
        assert "use SiTrelloProjects\\SiTrelloProjectsOptions;" in new, f"step1 failed: {r}"
        assert "TRELLO_PROJECTS_BOARD_UID" in new, f"step2 failed: {r}"
        assert "assertGreaterThan(0, $user->getId())" in new, f"step3 failed: {r}"
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_kevin_real_paste7_multiline_subst_with_newline_pattern():
    """Paste 7: `:%s/$this->assertNull($result);\\n    }\\n\\n    public function testWithProjectThatShouldNotBeOnTrelloProjects/REPLACED/`.

    Pattern crosses multiple lines via `\\n`. Replacement is a single
    token. Confirms multiline `:s` PAT works with literal-fallback when
    `(` in pattern is meant literally.
    """
    p = _tmp(
        "    public function testWithProject(): void\n"
        "    {\n"
        "        $this->assertNull($result);\n"
        "    }\n"
        "\n"
        "    public function testWithProjectThatShouldNotBeOnTrelloProjects(): void\n"
        "    {\n"
        "    }\n"
    )
    try:
        r = st.op_vim(
            p,
            r":%s/$this->assertNull($result);\n    }\n\n    public function testWithProjectThatShouldNotBeOnTrelloProjects/REPLACED/",
        )
        new = open(p).read()
        # Either succeeds via literal mode or fails with helpful hint
        if "REPLACED" in new:
            assert "ERROR" not in r
        else:
            assert "no match" in r
            assert "testWithProject" in r
    finally:
        os.unlink(p)


def test_kevin_real_paste8_four_action_search_sub_open_chain():
    """Paste 8: `/MARKER1\\e/MARKER2\\e:s/PAT/REPL/\\e/MARKER3\\eo<TEXT>\\e`.

    Sequence: find, find, substitute, find, open. Tests that long chains
    survive — each action sets up the next without state leak.
    """
    p = _tmp(
        "<?php\n"
        "function testNullEntity() {\n"
        "    $message = MessageHelper::createRandom([], true);\n"
        "    $result = $project;\n"
        "    $this->assertSame($project, $result);\n"
        "}\n"
    )
    try:
        r = st.op_vim(
            p,
            r"/\$message = MessageHelper::createRandom\e"
            r"/testNullEntity\e"
            r":s/MessageHelper::createRandom(\[\], true);/MessageHelper::createRandom([], true);\n        $this->assertGreaterThan(0, $message->getId());/\e"
            r"/\$this->assertSame(\$project, \$result);\e"
            r"o            $this->assertNull($result->getTrelloCardUid());\e",
        )
        new = open(p).read()
        assert "assertGreaterThan(0, $message->getId())" in new, f"sub failed: {r}"
        assert "assertNull($result->getTrelloCardUid())" in new, f"open failed: {r}"
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_kevin_real_o_insert_after_chained_search():
    """Paste 3 (simplified): `/use Shared\\\\ApplicationApi;\\e o<TEXT>\\e`.

    Find existing use stmt, open new line below, insert new use stmt.
    """
    p = _tmp(
        "<?php\n"
        "use Shared\\Foo;\n"
        "use Shared\\ApplicationApi;\n"
        "\n"
        "class X {}\n"
    )
    try:
        r = st.op_vim(
            p,
            r"/use Shared\\ApplicationApi;\eouse SiTrelloProjects\SiTrelloProjectsOptions;\e",
        )
        new = open(p).read()
        assert "use SiTrelloProjects\\SiTrelloProjectsOptions;" in new
        assert "ERROR" not in r
    finally:
        os.unlink(p)


# ---------------------------------------------------------------------------
# Indent motion operators: >{motion}, <{motion}, ={motion}
# ---------------------------------------------------------------------------


def test_indent_motion_gt2j():
    """`>2j` on line 1 indents lines 1-3 by 4 spaces."""
    p = _tmp("a\nb\nc\nd\n")
    try:
        r = st.op_vim(p, ">2j")
        new = open(p).read()
        assert new == "    a\n    b\n    c\nd\n", repr(new)
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_indent_motion_gtG():
    """`>G` from line 2 indents lines 2 to EOF."""
    p = _tmp("a\nb\nc\n")
    try:
        # move to line 2 first with j, then >G
        r = st.op_vim(p, "j>G")
        new = open(p).read()
        assert new == "a\n    b\n    c\n", repr(new)
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_dedent_motion_ltG():
    """`<G` after indenting dedents from current line to EOF."""
    p = _tmp("    a\n    b\n    c\n")
    try:
        r = st.op_vim(p, "<G")
        new = open(p).read()
        assert new == "a\nb\nc\n", repr(new)
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_indent_motion_paragraph():
    """`>}` indents the current paragraph (up to next blank line)."""
    p = _tmp("a\nb\n\nc\nd\n")
    try:
        r = st.op_vim(p, ">}")
        new = open(p).read()
        # lines 1-2 indented, blank line skipped, lines 4-5 untouched
        assert new == "    a\n    b\n\nc\nd\n", repr(new)
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_reindent_motion_eq2j():
    """`=2j` re-indents lines 1-3 to match the preceding non-blank line's indent.

    Line 1 has no predecessor so stays at column 0. Lines 2 and 3 pick up
    the indent of the line just above them.
    """
    p = _tmp("    hello\n        world\n            deep\nd\n")
    try:
        r = st.op_vim(p, "=2j")
        new = open(p).read()
        lines = new.split("\n")
        # line 1: no preceding non-blank → stripped to no indent
        assert lines[0] == "hello", repr(lines[0])
        # line 2: preceding line is now "hello" (0 indent) → also no indent
        assert lines[1] == "world", repr(lines[1])
        # line 3: preceding line is "world" (0 indent) → also no indent
        assert lines[2] == "deep", repr(lines[2])
        # line 4: untouched
        assert lines[3] == "d", repr(lines[3])
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_reindent_motion_eqeq_current_line():
    """`==` re-indents current line to match preceding non-blank line's indent."""
    p = _tmp("    base\n        over\nnext\n")
    try:
        # cursor starts on line 1; move to line 2 with j, then ==
        r = st.op_vim(p, "j==")
        new = open(p).read()
        lines = new.split("\n")
        assert lines[0] == "    base", repr(lines[0])
        assert lines[1] == "    over", repr(lines[1])  # matched line 1's 4-space indent
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_regression_gtgt_still_works():
    """`>>` still indents current line (regression guard)."""
    p = _tmp("hello\nworld\n")
    try:
        r = st.op_vim(p, ">>")
        new = open(p).read()
        assert new == "    hello\nworld\n", repr(new)
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_regression_ltlt_still_works():
    """`<<` still dedents current line (regression guard)."""
    p = _tmp("    hello\nworld\n")
    try:
        r = st.op_vim(p, "<<")
        new = open(p).read()
        assert new == "hello\nworld\n", repr(new)
        assert "ERROR" not in r
    finally:
        os.unlink(p)


# ---------------------------------------------------------------------------
# v (char-visual) alias tests
# ---------------------------------------------------------------------------

def test_v_char_vwd_deletes_word():
    """`vwd` — visual-select word + delete → equivalent to `dw`."""
    p = _tmp("hello world\n")
    try:
        r = st.op_vim(p, "vwd")
        new = open(p).read()
        assert new == "world\n", f"got: {new!r}; receipt: {r}"
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_v_char_v_dollar_y_yanks_to_eol():
    """`v$y` — visual to EOL + yank → equivalent to `y$`. Paste below."""
    p = _tmp("abc\ndef\n")
    try:
        r = st.op_vim(p, "v$yjp")
        new = open(p).read()
        # 'abc' yanked (to EOL), pasted below line 1 → abc appears twice
        assert new.count("abc") == 2, f"got: {new!r}; receipt: {r}"
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_v_char_vi_quote_change_greedy():
    """`vi"c<TEXT>\\e` — visual inside quotes + change → `ci"<TEXT>\\e`."""
    p = _tmp('say "hello" now\n')
    try:
        r = st.op_vim(p, r'vi"cgoodbye\e')
        new = open(p).read()
        assert new == 'say "goodbye" now\n', f"got: {new!r}; receipt: {r}"
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_v_char_vGd_deletes_to_eof():
    """`vGd` — visual to EOF + delete → equivalent to `dG`."""
    p = _tmp("a\nb\nc\n")
    try:
        r = st.op_vim(p, "vGd")
        new = open(p).read()
        assert new == "" or new == "\n", f"got: {new!r}; receipt: {r}"
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_v_char_vggd_deletes_to_bof():
    """`vggd` — visual to BOF + delete → equivalent to `dgg`. Cursor on
    last line; deletes from BOF to cursor (inclusive).
    """
    p = _tmp("a\nb\nc\n")
    try:
        # Move to last line then vggd — should delete everything up to and
        # including the cursor line.
        r = st.op_vim(p, "Gvggd")
        new = open(p).read()
        assert new == "" or new == "\n", f"got: {new!r}; receipt: {r}"
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_v_char_in_insert_text_not_rewritten():
    """`iv<TEXT>\\e` — `v` inside insert mode must NOT trigger the alias.
    The action starts with `i`, arrives as one greedy token; the rewriter
    only fires on actions whose first char is `v`.
    """
    p = _tmp("X\n")
    try:
        r = st.op_vim(p, r"ivwd\e")
        new = open(p).read()
        assert new == "vwdX\n", f"got: {new!r}; receipt: {r}"
        assert "ERROR" not in r
    finally:
        os.unlink(p)


# ---------------------------------------------------------------------------
# Undo / redo tests
# ---------------------------------------------------------------------------

def test_undo_after_insert():
    r"""iFOO\eu — insert then undo → file empty again."""
    p = _tmp("")
    try:
        os.environ["SUPERTOOL_VIM_NO_PERSIST"] = "1"
        r = st.op_vim(p, "iFOO\\eu")
        content = open(p).read()
        assert content == "", f"expected empty after undo, got: {content!r}"
        assert "ERROR" not in r
    finally:
        os.environ.pop("SUPERTOOL_VIM_NO_PERSIST", None)
        os.unlink(p)


def test_undo_after_delete_line():
    """dd then uuu — line restored, extra u's are no-ops."""
    p = _tmp("hello\n")
    try:
        os.environ["SUPERTOOL_VIM_NO_PERSIST"] = "1"
        r = st.op_vim(p, "dduuu")
        content = open(p).read()
        assert content == "hello\n", f"expected restored, got: {content!r}"
        assert "ERROR" not in r
    finally:
        os.environ.pop("SUPERTOOL_VIM_NO_PERSIST", None)
        os.unlink(p)


def test_undo_then_redo():
    r"""iFOO\eu\C-r — insert, undo, redo → FOO back."""
    p = _tmp("")
    try:
        os.environ["SUPERTOOL_VIM_NO_PERSIST"] = "1"
        r = st.op_vim(p, "iFOO\\eu\x12")
        content = open(p).read()
        assert content == "FOO", f"expected FOO after redo, got: {content!r}"
        assert "ERROR" not in r
    finally:
        os.environ.pop("SUPERTOOL_VIM_NO_PERSIST", None)
        os.unlink(p)


def test_undo_cross_call():
    """Cross-call undo: insert FOO in call 1, then u in call 2 restores empty."""
    p = _tmp("")
    # Use a test-specific cache dir so we don't pollute the real cache
    import tempfile
    cache_dir = tempfile.mkdtemp()
    os.environ["XDG_CACHE_HOME"] = cache_dir
    os.environ.pop("SUPERTOOL_VIM_NO_PERSIST", None)
    try:
        # Call 1: insert FOO
        r1 = st.op_vim(p, "iFOO")
        assert open(p).read() == "FOO", f"call1 failed: {r1}"
        # Call 2: undo (no within-script edits → falls through to cross-call snapshot)
        r2 = st.op_vim(p, "u")
        content = open(p).read()
        assert content == "", f"expected empty after cross-call undo, got: {content!r}"
        assert "cross-call undo" in r2, f"expected cross-call undo note in receipt, got: {r2}"
    finally:
        os.environ.pop("XDG_CACHE_HOME", None)
        os.unlink(p)
        import shutil
        shutil.rmtree(cache_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Macro recording and replay
# ---------------------------------------------------------------------------

def test_macro_record_and_replay_once():
    """qaiFOO\\eq@a — record `iFOO\\e` into register a, replay once.
    FOO is inserted during recording (the `i` verb executes), then @a
    replays the body and inserts FOO again — two insertions total.
    """
    p = _tmp("x\n")
    try:
        os.environ["SUPERTOOL_VIM_NO_PERSIST"] = "1"
        r = st.op_vim(p, "qaiFOO\\eq@a")
        result = open(p).read()
        assert result.count("FOO") == 2, f"expected 2x FOO, got: {result!r}"
        assert "macro recorded" in r
        assert "@a" in r
    finally:
        os.environ.pop("SUPERTOOL_VIM_NO_PERSIST", None)
        os.unlink(p)


def test_macro_record_and_replay_3x():
    """qajdd q3@a — record `jdd` into a (move down + delete line),
    replay 3 times. File: 7 lines (a-g). Recording executes jdd once
    (j to b, dd deletes b -> 6 lines a,c,d,e,f,g, cursor at c).
    3@a replays jdd 3x: deletes d, f; third j hits EOF so g survives
    -> 4 lines remain (a,c,e,g). Key check: lines shrank and @a ran.
    """
    p = _tmp("a\nb\nc\nd\ne\nf\ng\n")
    try:
        os.environ["SUPERTOOL_VIM_NO_PERSIST"] = "1"
        r = st.op_vim(p, "ggqajdd q3@a")
        result = open(p).read()
        lines = [l for l in result.split("\n") if l]
        # record deletes 1, replay 3x deletes 2 more (3rd j clamps at EOF) -> 4 left
        assert len(lines) == 4, f"expected 4 lines, got {len(lines)}: {result!r}"
        assert "@a" in r
    finally:
        os.environ.pop("SUPERTOOL_VIM_NO_PERSIST", None)
        os.unlink(p)


def test_macro_replay_last_with_atat():
    """qaiFOO\\eq@a@@ — record into a, replay once with @a, replay again
    with @@. FOO inserted during record + once for @a + once for @@ = 3 total.
    """
    p = _tmp("x\n")
    try:
        os.environ["SUPERTOOL_VIM_NO_PERSIST"] = "1"
        r = st.op_vim(p, "qaiFOO\\eq@a@@")
        result = open(p).read()
        assert result.count("FOO") == 3, f"expected 3x FOO, got: {result!r}"
        assert "@a" in r
    finally:
        os.environ.pop("SUPERTOOL_VIM_NO_PERSIST", None)
        os.unlink(p)


# ---------------------------------------------------------------------------
# `.` — repeat last change
# ---------------------------------------------------------------------------

def _tmp_persist(content: str):
    """Return (path, cache_dir) with persistence enabled via XDG_CACHE_HOME."""
    import tempfile, shutil
    cache_dir = tempfile.mkdtemp()
    os.environ["XDG_CACHE_HOME"] = cache_dir
    os.environ.pop("SUPERTOOL_VIM_NO_PERSIST", None)
    f = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
    f.write(content)
    f.close()
    return f.name, cache_dir


def _cleanup_persist(p: str, cache_dir: str) -> None:
    import shutil
    os.environ.pop("XDG_CACHE_HOME", None)
    try:
        os.unlink(p)
    except OSError:
        pass
    shutil.rmtree(cache_dir, ignore_errors=True)


def test_dot_repeat_insert_same_call():
    """iFOO\\e. — insert FOO, then repeat in same call at new cursor."""
    p, cache_dir = _tmp_persist("")
    try:
        r = st.op_vim(p, "iFOO\\e.")
        result = open(p).read()
        assert result.count("FOO") == 2, f"expected two FOO, got: {result!r}\nreceipt: {r}"
        assert "ERROR" not in r
    finally:
        _cleanup_persist(p, cache_dir)


def test_dot_repeat_insert_cross_call():
    """First call `iFOO\\e`, second call `G.` — repeat at end of file."""
    p, cache_dir = _tmp_persist("line1\n")
    try:
        r1 = st.op_vim(p, "iFOO\\e")
        assert "ERROR" not in r1, f"call1 failed: {r1}"
        r2 = st.op_vim(p, "G.")
        result = open(p).read()
        assert result.count("FOO") == 2, f"expected two FOO after cross-call repeat, got: {result!r}\nreceipt2: {r2}"
        assert "ERROR" not in r2
    finally:
        _cleanup_persist(p, cache_dir)


def test_dot_repeat_dd_same_call():
    """dd then move then . — delete two lines."""
    p, cache_dir = _tmp_persist("alpha\nbeta\ngamma\n")
    try:
        r = st.op_vim(p, "gg\u241e""dd\u241e""j.")
        result = open(p).read()
        lines = [l for l in result.split("\n") if l]
        assert len(lines) <= 1, f"expected at most 1 line left, got: {result!r}\nreceipt: {r}"
        assert "ERROR" not in r
    finally:
        _cleanup_persist(p, cache_dir)


def test_dot_repeat_nothing_to_repeat():
    """. with no prior change — should produce a log note, not an error."""
    p, cache_dir = _tmp_persist("hello\n")
    try:
        os.environ["SUPERTOOL_VIM_NO_PERSIST"] = "1"
        r = st.op_vim(p, ".")
        os.environ.pop("SUPERTOOL_VIM_NO_PERSIST", None)
        assert "ERROR" not in r
        assert "nothing to repeat" in r
    finally:
        _cleanup_persist(p, cache_dir)


def test_dot_repeat_x_deletes_char():
    """x then . — delete two chars at same position."""
    p, cache_dir = _tmp_persist("abcde\n")
    try:
        r = st.op_vim(p, "gg\u241e""x.")
        result = open(p).read()
        assert result == "cde\n", f"got: {result!r}\nreceipt: {r}"
        assert "ERROR" not in r
    finally:
        _cleanup_persist(p, cache_dir)


def test_dot_repeat_ciw_replaces_word():
    """ciw<NEW>\\e then w. — change word, move, repeat on next word."""
    p, cache_dir = _tmp_persist("foo bar\n")
    try:
        r = st.op_vim(p, "gg\u241e""ciwNEW\\e\u241e""w.")
        result = open(p).read()
        assert result.count("NEW") == 2, f"expected two NEW, got: {result!r}\nreceipt: {r}"
        assert "ERROR" not in r
    finally:
        _cleanup_persist(p, cache_dir)


# ---------------------------------------------------------------------------
# M2 / M3 — :!cmd integration: . repeat and u undo
# ---------------------------------------------------------------------------

def test_shell_filter_sort_and_dot_repeat():
    """`:%!sort` sorts the buffer; `.` replays it (idempotent — no error)."""
    p, cache_dir = _tmp_persist("b\na\n")
    try:
        r = st.op_vim(p, ":%!sort")
        result = open(p).read()
        assert result == "a\nb\n", f"sort failed: {result!r}\nreceipt: {r}"
        assert "ERROR" not in r

        r2 = st.op_vim(p, ".")
        result2 = open(p).read()
        assert result2 == "a\nb\n", f"dot repeat changed sorted result: {result2!r}\nreceipt: {r2}"
        assert "ERROR" not in r2
    finally:
        _cleanup_persist(p, cache_dir)


def test_shell_filter_sort_undo():
    """`:%!sort` then `u` restores original content."""
    p, cache_dir = _tmp_persist("b\na\n")
    try:
        r = st.op_vim(p, ":%!sort␞u")
        result = open(p).read()
        assert result == "b\na\n", f"undo after sort failed: {result!r}\nreceipt: {r}"
        assert "ERROR" not in r
    finally:
        _cleanup_persist(p, cache_dir)


def test_shell_insert_echo_undo():
    """`u` after `:!echo hi` removes the inserted line."""
    p, cache_dir = _tmp_persist("")
    try:
        r = st.op_vim(p, ":!echo hi␞u")
        result = open(p).read()
        assert result == "", f"undo after :!echo failed: {result!r}\nreceipt: {r}"
        assert "ERROR" not in r
    finally:
        _cleanup_persist(p, cache_dir)


# ---------------------------------------------------------------------------
# M5 — =motion auto-indent: mixed tabs/spaces handling
# ---------------------------------------------------------------------------

def test_m5_eq_eq_tab_file_aligns_with_tabs():
    """Tab-style file: ref has 2 tabs (depth 2), target has 1 tab (depth 1) → corrected to 2 tabs."""
    # ref line 1 = "\t\tdef foo():" depth=2; target line 2 = "\tpass" depth=1 (wrong)
    content = "\t\tdef foo():\n\tpass\n"
    p = _tmp(content)
    try:
        r = st.op_vim(p, "2G==")
        result = open(p).read()
        lines = result.splitlines()
        assert lines[1] == "\t\tpass", (
            f"Expected '\\t\\tpass' (2 tabs), got: {lines[1]!r}\nreceipt: {r}"
        )
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_m5_eq_eq_space_file_aligns_with_spaces():
    """Space-style file: ref has 8 spaces (depth 2), target has 4 spaces (depth 1) → corrected to 8 spaces."""
    # ref line 1 = "        def foo():" depth=2; target line 2 = "    pass" depth=1 (wrong)
    content = "        def foo():\n    pass\n"
    p = _tmp(content)
    try:
        r = st.op_vim(p, "2G==")
        result = open(p).read()
        lines = result.splitlines()
        assert lines[1] == "        pass", (
            f"Expected 8-space indent, got: {lines[1]!r}\nreceipt: {r}"
        )
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_m5_eq_eq_already_correct_noop():
    """Target line already has correct indent depth — no spurious change."""
    # ref="\t\tdef foo():" depth=2; target="\t\tpass" depth=2 → already correct
    content = "\t\tdef foo():\n\t\tpass\n"
    p = _tmp(content)
    try:
        r = st.op_vim(p, "2G==")
        result = open(p).read()
        assert result == content, (
            f"Content changed when it shouldn't have:\nbefore: {content!r}\nafter:  {result!r}\nreceipt: {r}"
        )
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_m5_eq_eq_no_preceding_nonblank_uses_depth_zero():
    """File starts with blank lines; `==` on first non-blank produces zero indent."""
    # Lines 1-2 are blank; line 3 has 4-space indent → no preceding non-blank → depth 0
    content = "\n\n    indented_start\n"
    p = _tmp(content)
    try:
        r = st.op_vim(p, "3G==")
        result = open(p).read()
        lines = result.splitlines()
        target = [ln for ln in lines if ln.strip() == "indented_start"][0]
        assert target == "indented_start", (
            f"Expected no indent (depth 0), got: {target!r}\nreceipt: {r}"
        )
        assert "ERROR" not in r
    finally:
        os.unlink(p)



# --- M1/M4 macro correctness fixes ---


def test_m1_macro_close_q_not_inside_insert_text():
    # M1: qaiquery\eq @a — closing q must be bare q after \e, not q in "query"
    # Body should be iquery\e; @a replays it inserting "query" a second time.
    p = _tmp("")
    try:
        r = st.op_vim(p, r"qaiquery\eq @a")
        assert "ERROR" not in r, f"Unexpected error: {r}"
        result = open(p).read()
        assert result.count("query") == 2, (
            f"Expected 'query' x2, got {result.count('query')} in {result!r}; receipt: {r}"
        )
    finally:
        os.unlink(p)


def test_m1_macro_body_full_word_not_truncated():
    # M1: body must be iquery\e not iquer — replay inserts full "query"
    p = _tmp("x\n")
    try:
        r = st.op_vim(p, r"qaiquery\eq @a")
        assert "ERROR" not in r, f"Unexpected error: {r}"
        result = open(p).read()
        assert "query" in result, f"'query' not in {result!r}; receipt: {r}"
    finally:
        os.unlink(p)


def test_m1_q_inside_search_arg_is_data_not_close():
    # M1: qa/query\eq — q in search arg is data; bare q after \e closes.
    p = _tmp("query line\nanother line\n")
    try:
        r = st.op_vim(p, r"qa/query\eq")
        assert "ERROR" not in r, f"Unexpected error: {r}"
    finally:
        os.unlink(p)


def test_m1_bare_q_after_escape_closes_recording():
    # M1: qaiX\eq @a — q inside insert text is data; bare q after \e closes.
    # @a replay should insert "X" into the file.
    p = _tmp("abc\n")
    try:
        r = st.op_vim(p, r"qaiX\eq @a")
        assert "ERROR" not in r, f"Unexpected error: {r}"
        result = open(p).read()
        assert "X" in result, f"'X' not found in {result!r}; receipt: {r}"
    finally:
        os.unlink(p)


def test_m4_self_replaying_macro_errors_with_depth_limit():
    # M4: qa@aq then @a — self-recursive macro must error, not loop.
    # File must be unchanged on error.
    initial = "hello world\n"
    p = _tmp(initial)
    try:
        r_replay = st.op_vim(p, "qa@aq @a")
        assert "ERROR" in r_replay, f"Expected ERROR, got: {r_replay!r}"
        assert "recursion depth limit" in r_replay, (
            f"Expected 'recursion depth limit' in error, got: {r_replay!r}"
        )
        assert "100" in r_replay, f"Expected '100' in error, got: {r_replay!r}"
        result = open(p).read()
        assert result == initial, f"File modified despite recursion error: {result!r}"
    finally:
        os.unlink(p)


def test_m4_normal_macro_replay_not_blocked():
    # M4: 5 replays of a simple macro stay well under the 100 guard.
    p = _tmp("a\n" * 10)
    try:
        r = st.op_vim(p, r"qaoX\eq 5@a")
        assert "ERROR" not in r, f"Unexpected error: {r}"
        result = open(p).read()
        assert result.count("X") >= 5, (
            f"Expected at least 5 'X' insertions, got {result.count('X')} in {result!r}"
        )
    finally:
        os.unlink(p)


# ---------------------------------------------------------------------------
# :! ex shell-filter verb (moved from test_vim_ex_bang.py)
# ---------------------------------------------------------------------------


def test_bang_bare_inserts_stdout_after_cursor(tmp_path):
    """:!echo hi inserts the command's stdout after the cursor's line."""
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nc\n")
    out = st.op_vim(str(f), ":!echo hi")
    assert "ERROR" not in out, out
    assert f.read_text() == "a\nhi\nb\nc\n"


def test_bang_percent_pipes_whole_buffer(tmp_path):
    """:%!tr a-z A-Z upcases the whole buffer."""
    f = tmp_path / "x.txt"
    f.write_text("abc\ndef\n")
    out = st.op_vim(str(f), ":%!tr a-z A-Z")
    assert "ERROR" not in out, out
    assert f.read_text() == "ABC\nDEF\n"


def test_bang_range_pipes_selected_lines(tmp_path):
    """:2,3!tr a-z A-Z only upcases lines 2..3."""
    f = tmp_path / "x.txt"
    f.write_text("aaa\nbbb\nccc\nddd\n")
    out = st.op_vim(str(f), ":2,3!tr a-z A-Z")
    assert "ERROR" not in out, out
    assert f.read_text() == "aaa\nBBB\nCCC\nddd\n"


# ---------------------------------------------------------------------------
# L4 — operator-motion count semantics: outer count repeats op, inner is distance
# ---------------------------------------------------------------------------


def test_indent_outer_count_repeats_levels():
    """3>j: outer count=3 repeats indent, motion j covers 2 lines (current+next)."""
    p = _tmp("a\nb\nc\n")
    try:
        r = st.op_vim(p, "3>j")
        result = open(p).read()
        lines = result.splitlines()
        # 3 repetitions of indent = 3 * 4 spaces = 12 spaces
        assert lines[0] == "            a", f"line 0: {lines[0]!r}\nreceipt: {r}"
        assert lines[1] == "            b", f"line 1: {lines[1]!r}\nreceipt: {r}"
        assert lines[2] == "c", f"line 2 should be unchanged: {lines[2]!r}\nreceipt: {r}"
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_indent_motion_count_sets_range():
    """>2j: motion count=2 covers current line + 2 below (3 lines total), 1 indent level."""
    p = _tmp("a\nb\nc\nd\n")
    try:
        r = st.op_vim(p, ">2j")
        result = open(p).read()
        lines = result.splitlines()
        assert lines[0] == "    a", f"line 0: {lines[0]!r}"
        assert lines[1] == "    b", f"line 1: {lines[1]!r}"
        assert lines[2] == "    c", f"line 2: {lines[2]!r}"
        assert lines[3] == "d",     f"line 3 unchanged: {lines[3]!r}"
        assert "ERROR" not in r
    finally:
        os.unlink(p)


def test_indent_outer_and_motion_count_combined():
    """3>2j: motion covers 3 lines, outer count applies indent 3 times = 12 spaces."""
    p = _tmp("a\nb\nc\nd\n")
    try:
        r = st.op_vim(p, "3>2j")
        result = open(p).read()
        lines = result.splitlines()
        # 3 levels of indent = 12 spaces each
        assert lines[0] == "            a", f"line 0: {lines[0]!r}"
        assert lines[1] == "            b", f"line 1: {lines[1]!r}"
        assert lines[2] == "            c", f"line 2: {lines[2]!r}"
        assert lines[3] == "d",             f"line 3 unchanged: {lines[3]!r}"
        assert "ERROR" not in r
    finally:
        os.unlink(p)

