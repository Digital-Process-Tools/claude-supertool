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


def test_single_bracket_json_array_still_json():
    assert supertool._detect_payload_format('[{"path": "a"}]') == "json"


def test_json_object_still_json():
    assert supertool._detect_payload_format('{"path": "a"}') == "json"
