"""A `FIELD = @rest` header line ends TOML parsing there and hands
everything after it, byte for byte, as FIELD's value (#1868).

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
