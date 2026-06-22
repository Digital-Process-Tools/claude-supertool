"""#287 — zero-hit literal fallback for grep/around/grep_around.

A pattern like `.html(json.data)` compiles as a valid regex (`.` wildcard,
`(...)` group) but never matches the literal code fragment, and the existing
re.error fallback never fires because there is no compile error. These tests
pin the behaviour: on zero regex hits for a regexy pattern, retry as a literal
and surface a one-line note. Plain words and genuinely-absent terms stay clean.
"""

from __future__ import annotations

from pathlib import Path

import supertool


FRAGMENT = ".html(json.data)"


def _js(tmp_path: Path) -> Path:
    f = tmp_path / "a.js"
    f.write_text("foo = obj.html(json.data);\nbar = 1;\n")
    return f


def test_grep_literal_fallback_on_metachar_fragment(tmp_path: Path) -> None:
    f = _js(tmp_path)
    out = supertool.op_grep(FRAGMENT, str(f))
    assert "no regex match" in out
    assert "1 literal match(es)" in out
    assert "1:foo = obj.html(json.data);" in out


def test_grep_context_literal_fallback(tmp_path: Path) -> None:
    f = _js(tmp_path)
    out = supertool.op_grep(FRAGMENT, str(f), context=2)
    assert "no regex match" in out
    assert "1:foo = obj.html(json.data);" in out


def test_grep_count_only_literal_fallback(tmp_path: Path) -> None:
    f = _js(tmp_path)
    out = supertool.op_grep(FRAGMENT, str(f), count_only=True)
    assert "no regex match" in out
    assert "1 total matches" in out


def test_around_file_literal_fallback(tmp_path: Path) -> None:
    f = _js(tmp_path)
    out = supertool.op_around(FRAGMENT, str(f), 2)
    assert "no regex match" in out
    assert "obj.html(json.data)" in out


def test_around_dir_literal_fallback(tmp_path: Path) -> None:
    _js(tmp_path)
    out = supertool.op_around(FRAGMENT, str(tmp_path), 2)
    assert "no regex match" in out
    assert "obj.html(json.data)" in out


def test_no_note_when_regex_matches(tmp_path: Path) -> None:
    f = tmp_path / "a.js"
    f.write_text("foo bar baz\n")
    out = supertool.op_grep("foo.*baz", str(f))
    assert "no regex match" not in out
    assert "(1 results" in out


def test_no_note_when_truly_absent_regexy(tmp_path: Path) -> None:
    f = _js(tmp_path)
    out = supertool.op_grep("absent.token()", str(f))
    assert "no regex match" not in out
    assert "(0 results" in out


def test_no_retry_for_plain_word(tmp_path: Path) -> None:
    f = _js(tmp_path)
    out = supertool.op_grep("zzznope", str(f))
    assert "no regex match" not in out
    assert "(0 results" in out


def test_is_regexy_gate() -> None:
    assert supertool._is_regexy(".html(json.data)")
    assert supertool._is_regexy("a|b")
    assert not supertool._is_regexy("plainword")
    assert not supertool._is_regexy("snake_case_name")
