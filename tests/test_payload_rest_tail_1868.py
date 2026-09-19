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


def test_rest_tail_marker_tolerates_a_crlf_line_ending() -> None:
    # A marker line terminated \r\n (a CRLF-authored payload piped through
    # stdin, which does not get universal-newline translation the way a
    # plain `open()` read does) must still be recognised (oss:auditor
    # finding, #1868 self-review).
    raw = 'path = "x.py"\r\ncontent = @rest\r\ndef f():\r\n    return 1\r\n'
    m = supertool._AT_FILE_REST_MARKER_RE.search(raw)
    assert m is not None
    assert m.group(1) == "content"
