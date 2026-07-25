"""Regression test for issue #357.

A payload whose first non-whitespace is a TOML table-array header `[[ops]]`
must be detected as TOML, not JSON. A leading `[[` is never valid JSON.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import supertool


TOML_OPS = """[[ops]]
path = "a.py"
old = '''foo'''
new = '''bar'''

[[ops]]
path = "b.py"
old = '''baz'''
new = '''qux'''
"""


def test_double_bracket_detected_as_toml():
    assert supertool._detect_payload_format("[[ops]]") == "toml"


def test_double_bracket_leading_whitespace_detected_as_toml():
    assert supertool._detect_payload_format("\n  \t[[ops]]\n") == "toml"


def test_load_at_file_parses_toml_ops_array():
    with tempfile.NamedTemporaryFile(
        "w", suffix=".toml", delete=False, encoding="utf-8"
    ) as f:
        f.write(TOML_OPS)
        path = f.name
    try:
        result = supertool._load_at_file("@" + path)
    finally:
        os.unlink(path)
    assert isinstance(result, dict)
    assert "ops" in result
    assert [o["path"] for o in result["ops"]] == ["a.py", "b.py"]


def test_fallback_parser_handles_table_arrays():
    """The <3.11 path must parse [[ops]] too, not just the tomllib path.

    test_load_at_file_parses_toml_ops_array goes through whichever parser the
    running interpreter selects, so on 3.11+ it never touches the fallback —
    which is how `[[ops]]` shipped working on 3.11/3.12 and failing on 3.9/3.10
    with `bad key at offset 0`. This calls the fallback directly, on every
    version.
    """
    result = supertool._mini_toml_loads(TOML_OPS)

    assert [o["path"] for o in result["ops"]] == ["a.py", "b.py"]
    assert result["ops"][0]["old"] == "foo"
    assert result["ops"][1]["new"] == "qux"


def test_fallback_parser_still_reads_top_level_keys():
    """A plain single-op payload has no table header and must be unaffected."""
    result = supertool._mini_toml_loads('path = "a.py"\nold = \'x\'\nnew = \'y\'')

    assert result == {"path": "a.py", "old": "x", "new": "y"}


def test_fallback_parser_rejects_single_table_header_clearly():
    """`[table]` is unsupported — say so, rather than `bad key at offset 0`."""
    try:
        supertool._mini_toml_loads("[single]\nk = 1\n")
    except ValueError as exc:
        assert "not supported" in str(exc)
        assert "[[table]]" in str(exc) or "JSON" in str(exc)
    else:
        raise AssertionError("a single [table] header must raise")


def test_fallback_parser_rejects_unterminated_table_header():
    try:
        supertool._mini_toml_loads("[[ops\npath = 'a'\n")
    except ValueError as exc:
        assert "unterminated" in str(exc)
    else:
        raise AssertionError("an unterminated [[ header must raise")


def test_fallback_parser_rejects_name_clash_between_value_and_table():
    try:
        supertool._mini_toml_loads("ops = 1\n[[ops]]\npath = 'a'\n")
    except ValueError as exc:
        assert "both a value and a" in str(exc)
    else:
        raise AssertionError("a key reused as a table name must raise")


def test_single_bracket_json_array_still_json():
    assert supertool._detect_payload_format('[{"path": "a"}]') == "json"


def test_json_object_still_json():
    assert supertool._detect_payload_format('{"path": "a"}') == "json"
