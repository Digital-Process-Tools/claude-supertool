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


def test_kevin_real_ggV_unknown_verb():
    """Kevin's actual paste: `ggV:53s/...` — V is not supported."""
    p = _tmp("\n".join(f"line {i}" for i in range(60)) + "\n")
    try:
        r = st.op_vim(p, "ggV:53s/foo/bar/\\e")
        assert "unknown verb" in r
        assert "did you mean" in r.lower()
        # Should NOT dump the catalog
        assert r.count(",") < 15
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
    """Paste 2: `ggV:53s/.../.../` — V mid-chain.

    Should fail fast with did-you-mean, not 80-item wall.
    """
    p = _tmp("\n".join(f"line {i}" for i in range(60)) + "\n")
    try:
        r = st.op_vim(p, r"ggV:53s/foo/bar/\e")
        assert "unknown verb" in r
        # Verify the catalog dump is gone
        assert "ci{" not in r
        assert "did you mean" in r.lower()
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
