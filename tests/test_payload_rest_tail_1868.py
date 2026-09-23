"""A `FIELD = @rest` header line ends TOML parsing there and hands
everything after it as FIELD's value (#1868) -- byte for byte only for
`content` (`_AT_FILE_REST_RAW_FIELDS`); every other field has exactly one
trailing newline stripped by `_rest_tail_value` before it lands in the
parsed dict (#2668).

A literal (`'''`) block cannot carry a nested `'''`, and a doubled
backslash in a literal block trips the write-time refusal even when it
was meant as written. Both are TOML's own quoting rules colliding with
source code that happens to use the same characters. `@rest` sidesteps
the collision entirely: nothing after the marker line is ever
TOML-parsed, so there is nothing left to collide.
"""
from pathlib import Path

import pytest

import supertool


def _write(tmp_path: Path, text: str) -> str:
    p = tmp_path / "p.toml"
    p.write_text(text)
    return "@" + str(p)


def test_rest_tail_carries_nested_triple_quotes_verbatim(tmp_path: Path) -> None:
    tail = 'def f():\n    return """a nested \'\'\' triple """ + "\\n"\n'
    raw = 'path = "x.py"\ncontent = @rest\n' + tail
    ref = _write(tmp_path, raw)
    parsed = supertool._load_at_file(ref)
    assert parsed["content"] == tail
    assert parsed["path"] == "x.py"


def test_rest_tail_is_exempt_from_the_doubled_backslash_refusal(tmp_path: Path) -> None:
    # An even backslash run in a '''-block field would normally refuse
    # (#1087) -- the @rest tail is never parsed as a literal block at all,
    # so the guard has nothing to find.
    tail = "line one\\\\\nline two\n"
    raw = 'path = "x.py"\ncontent = @rest\n' + tail
    ref = _write(tmp_path, raw)
    parsed = supertool._load_at_file(ref)
    assert parsed["content"] == tail


def test_rest_tail_given_twice_refuses_naming_both(tmp_path: Path) -> None:
    raw = 'path = "x.py"\ncontent = "already here"\ncontent = @rest\ntail text\n'
    ref = _write(tmp_path, raw)
    with pytest.raises(ValueError) as excinfo:
        supertool._load_at_file(ref)
    message = str(excinfo.value)
    assert "content" in message
    assert "twice" in message
    assert "line 2" in message
    assert "line 3" in message


def test_rest_tail_empty_is_refused(tmp_path: Path) -> None:
    raw = 'path = "x.py"\ncontent = @rest\n'
    ref = _write(tmp_path, raw)
    with pytest.raises(ValueError) as excinfo:
        supertool._load_at_file(ref)
    assert "empty" in str(excinfo.value)


def test_rest_tail_without_trailing_newline_after_marker(tmp_path: Path) -> None:
    raw = 'path = "x.py"\ncontent = @rest'
    ref = _write(tmp_path, raw)
    with pytest.raises(ValueError) as excinfo:
        supertool._load_at_file(ref)
    assert "empty" in str(excinfo.value)


def test_payload_without_a_rest_marker_is_unaffected(tmp_path: Path) -> None:
    raw = 'path = "x.py"\ncontent = "hello"\n'
    ref = _write(tmp_path, raw)
    parsed = supertool._load_at_file(ref)
    assert parsed == {"path": "x.py", "content": "hello"}


def test_provenance_names_the_rest_tail() -> None:
    raw = 'path = "x.py"\ncontent = @rest\nbody\n'
    prov = supertool._payload_field_provenance(raw, "content")
    assert "@rest" in prov


def test_marker_inside_a_batch_table_array_is_refused_loudly(tmp_path: Path) -> None:
    # A `[[ops]]` table array before the marker means the marker line
    # belongs to a NESTED table, not the top-level dict this pre-split
    # assumes -- treating it as the header-ending marker would truncate
    # every later [[ops]] entry and misattribute the tail to the wrong
    # table, silently. The split must decline and let the literal `@rest`
    # token reach the TOML parser as an ordinary invalid value instead
    # (#1868 self-review).
    raw = (
        '[[ops]]\nop = "paste"\npath = "a.py"\ncontent = @rest\n'
        'def a(): pass\n\n[[ops]]\nop = "paste"\npath = "b.py"\n'
        'content = "whatever"\n'
    )
    ref = _write(tmp_path, raw)
    with pytest.raises(ValueError) as excinfo:
        supertool._load_at_file(ref)
    message = str(excinfo.value)
    assert "TOML parse error" in message


def test_duplicate_field_line_number_prefers_the_occurrence_nearest_the_marker(
        tmp_path: Path) -> None:
    # A decoy line that merely LOOKS like a `content =` assignment, sitting
    # inside an earlier string value, must not be reported as the
    # conflicting field ahead of the real header assignment closer to the
    # marker (#1868 self-review).
    raw = (
        'path = "x.py"\n'
        'note = """\ncontent = "decoy inside a string, not real"\nmore\n"""\n'
        'content = "already here"\ncontent = @rest\ntail\n'
    )
    ref = _write(tmp_path, raw)
    with pytest.raises(ValueError) as excinfo:
        supertool._load_at_file(ref)
    message = str(excinfo.value)
    assert "line 6" in message
    assert "line 3" not in message


def test_marker_inside_a_plain_table_section_is_refused_loudly(tmp_path: Path) -> None:
    # A single-bracket [section] header before the marker means the marker
    # belongs to that table, not the top level -- injecting the tail at the
    # top level regardless would silently drop the rest of that table's own
    # content and misplace the field (oss:auditor finding, #1868 self-review).
    raw = (
        'path = "x.py"\nold = "matches this in file"\n'
        'content = "some fixed value"\n[extra]\nfoo = @rest\nbar = 1\n'
    )
    ref = _write(tmp_path, raw)
    with pytest.raises(ValueError) as excinfo:
        supertool._load_at_file(ref)
    assert "TOML parse error" in str(excinfo.value)


def test_rest_tail_strips_trailing_newline_for_edit_new_field(tmp_path: Path) -> None:
    # `new = @rest` on an `edit`-shaped payload: the heredoc's own closing
    # newline is not part of the replacement text, and left in it turns
    # `old = "x = 1"` / `new = @rest` / tail `x = 9` into a spurious blank
    # line in the written file (#2668). `content` (paste/append) is the
    # only field that keeps the tail byte for byte -- see the positive
    # control above.
    tail = "x = 9\n"
    raw = 'old = "x = 1"\nnew = @rest\n' + tail
    ref = _write(tmp_path, raw)
    parsed = supertool._load_at_file(ref)
    assert parsed["new"] == "x = 9"


def test_rest_tail_strips_trailing_newline_for_grep_pattern_field(tmp_path: Path) -> None:
    # `pattern = @rest`: an unstripped trailing newline turns an exact
    # single-line pattern into one that matches every line (#2668).
    tail = "y = 2\n"
    raw = 'path = "."\npattern = @rest\n' + tail
    ref = _write(tmp_path, raw)
    parsed = supertool._load_at_file(ref)
    assert parsed["pattern"] == "y = 2"


def test_rest_tail_strips_only_one_trailing_newline(tmp_path: Path) -> None:
    # A tail with a genuinely blank last line (two trailing newlines)
    # keeps one of them -- only the heredoc's own closing newline is not
    # part of the value, not a blank line the payload author wrote on
    # purpose.
    tail = "x = 9\n\n"
    raw = 'old = "x = 1"\nnew = @rest\n' + tail
    ref = _write(tmp_path, raw)
    parsed = supertool._load_at_file(ref)
    assert parsed["new"] == "x = 9\n"


def test_rest_tail_without_trailing_newline_is_unaffected_by_stripping(
        tmp_path: Path) -> None:
    # A tail whose last line has no trailing newline at all (the payload
    # file itself was not newline-terminated) has nothing to strip.
    tail = "x = 9"
    raw = 'old = "x = 1"\nnew = @rest\n' + tail
    ref = _write(tmp_path, raw)
    parsed = supertool._load_at_file(ref)
    assert parsed["new"] == "x = 9"


def test_rest_tail_strips_a_crlf_trailing_newline_too(tmp_path: Path) -> None:
    # `_load_at_file`'s own `open(resolved, "r", encoding="utf-8")` call
    # (no `newline=` argument) reads in universal-newline mode, which
    # collapses ANY "\r\n" in the file to plain "\n" before `raw` is ever
    # built -- deterministically, on every platform, since this is
    # CPython io behaviour and not OS-conditioned. So this test alone
    # cannot tell the `\r\n`-stripping branch in `_rest_tail_value` apart
    # from an absent one, on any OS (oss:auditor finding, #2668
    # self-review). Kept as the end-to-end shape; the two tests below are
    # what actually pin the CRLF branch.
    #
    # `_write()` is deliberately NOT used here: it writes through
    # `Path.write_text()` with its default `newline=None`, which -- on
    # the *write* side -- translates every "\n" in the string to
    # `os.linesep` before it reaches disk. On POSIX `os.linesep` is "\n",
    # so that translation is a no-op and the intended "x = 9\r\n" lands on
    # disk unchanged. On Windows `os.linesep` is "\r\n", so the single
    # "\n" inside the "\r\n" tail this test builds is rewritten to "\r\n",
    # producing "x = 9\r\r\n" on disk -- a payload this test never meant
    # to write -- and CI caught the resulting platform-only failure
    # (windows-latest/3.12, #2668 review). `newline=""` on the write call
    # below disables that write-side translation, so the on-disk bytes
    # are exactly the string given, on every platform; the read-side
    # collapse described above then behaves identically everywhere too.
    tail = "x = 9\r\n"
    raw = 'old = "x = 1"\nnew = @rest\n' + tail
    p = tmp_path / "p.toml"
    # `Path.write_text`'s `newline` keyword needs Python 3.10+ (this repo's
    # floor is 3.9, pyproject.toml's `requires-python`) -- `open()`'s own
    # `newline` keyword has been there since 3.9, so use that directly
    # instead (caught by CI on all three 3.9 legs, #2668 review).
    with open(p, "w", newline="", encoding="utf-8") as f:
        f.write(raw)
    parsed = supertool._load_at_file("@" + str(p))
    assert parsed["new"] == "x = 9"


def test_rest_tail_value_strips_crlf_directly() -> None:
    # Whitebox, on purpose: calls the stripping helper directly so a
    # regression in the `\r\n`-before-`\n` branch fails here even though
    # every `@file`-route test above goes through a translation that
    # already collapsed `\r\n` to `\n` before this function ever ran
    # (#2668, oss:auditor finding).
    assert supertool._rest_tail_value("new", "x = 9\r\n") == "x = 9"
    assert supertool._rest_tail_value("new", "x = 9\r\n\r\n") == "x = 9\r\n"


def test_rest_tail_stdin_route_preserves_and_strips_crlf(monkeypatch) -> None:
    # `@-` (stdin) does NOT get universal-newline translation the way
    # `_write()`'s file round-trip does (confirmed directly: `sys.stdin`
    # here is an `io.StringIO`, which returns its bytes unchanged on
    # `.read()`) -- this is the one real `@file` route where a `\r\n`
    # tail actually reaches `_rest_tail_value` intact, and the auditor
    # finding was that nothing exercised it end to end (#2668).
    import io
    raw = 'old = "x = 1"\nnew = @rest\nx = 9\r\n'
    monkeypatch.setattr("sys.stdin", io.StringIO(raw))
    parsed = supertool._load_at_file("@-")
    assert parsed["new"] == "x = 9"


def test_rest_tail_refuses_when_empty_after_stripping(tmp_path: Path) -> None:
    # `new = @rest` followed by nothing but one blank line: `rest_tail`
    # itself is "\n", so the pre-existing empty-tail check (which runs
    # before stripping) does not catch it, and stripping that single
    # newline would silently hand a non-`content` field an empty string --
    # `edit`/`replace`'s `new` would delete the matched text, and `grep`'s
    # `pattern` would match every line, the exact silent-wrong-answer shape
    # #2668 was filed to eliminate (self-review, oss:developer review
    # round).
    raw = 'old = "x = 1"\nnew = @rest\n\n'
    ref = _write(tmp_path, raw)
    with pytest.raises(ValueError) as excinfo:
        supertool._load_at_file(ref)
    message = str(excinfo.value)
    assert "empty" in message
    assert "new" in message


def test_rest_tail_content_field_keeps_a_blank_line_tail(tmp_path: Path) -> None:
    # The refusal above is scoped to non-`content` fields: `content`
    # (paste/append/replace_lines) keeps the tail byte for byte on
    # purpose, and a file whose whole body is one blank line is a
    # legitimate thing to write.
    raw = 'path = "x.py"\ncontent = @rest\n\n'
    ref = _write(tmp_path, raw)
    parsed = supertool._load_at_file(ref)
    assert parsed["content"] == "\n"


def test_rest_tail_marker_tolerates_a_crlf_line_ending() -> None:
    # A marker line terminated \r\n (a CRLF-authored payload piped through
    # stdin, which does not get universal-newline translation the way a
    # plain `open()` read does) must still be recognised (oss:auditor
    # finding, #1868 self-review).
    raw = 'path = "x.py"\r\ncontent = @rest\r\ndef f():\r\n    return 1\r\n'
    m = supertool._AT_FILE_REST_MARKER_RE.search(raw)
    assert m is not None
    assert m.group(1) == "content"
