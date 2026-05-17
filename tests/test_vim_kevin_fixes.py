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
    """Mined from log 9abd0ba5 — Kevin actual paste:
    `:s/HasTagChecker::class, \\$checkerClass/$checkerClass, HasTagChecker::class/`.

    Over-escaped `\\$checkerClass` in PAT. Literal-fallback must strip
    `\\` → `\` → `` to get `$checkerClass`, then content.replace.
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
