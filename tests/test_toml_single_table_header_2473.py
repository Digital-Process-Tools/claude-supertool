r"""#2473 - the fallback TOML parser supports a single [table] header.

`_mini_toml_loads` (Python <3.11, no stdlib `tomllib`) already parses
`[[table]]` array-of-tables headers but raised on a bare `[table]` header
with "single [table] header ... not supported". That blocks every
@file/@- payload with a nested-object value on any op, not only json-set
(#1822, where it was first found).

Scope, per the issue: a single [table] header, bare name only -- the same
character set [[table]] already validates against (alnum, `_`, `-`; no
dots). Inline-table syntax (`{ ... }`) and dotted table headers are
explicitly out of scope.
"""
import supertool


def test_single_table_header_parses_to_nested_dict() -> None:
    raw = "[set]\nfoo = \"bar\"\n"
    assert supertool._mini_toml_loads(raw) == {"set": {"foo": "bar"}}


def test_single_table_header_with_multiple_keys() -> None:
    raw = "[options]\nverbose = true\ncount = 3\n"
    assert supertool._mini_toml_loads(raw) == {
        "options": {"verbose": True, "count": 3}
    }


def test_key_value_before_table_header_stays_at_top_level() -> None:
    raw = "top = 1\n[set]\nfoo = \"bar\"\n"
    assert supertool._mini_toml_loads(raw) == {
        "top": 1,
        "set": {"foo": "bar"},
    }


def test_array_of_tables_still_parses_after_the_change() -> None:
    """Positive control: [[table]] must still work, paired with the new
    [table] support rather than assumed unbroken."""
    raw = "[[ops]]\nop = \"paste\"\npath = \"a.py\"\n\n[[ops]]\nop = \"edit\"\npath = \"b.py\"\n"
    assert supertool._mini_toml_loads(raw) == {
        "ops": [
            {"op": "paste", "path": "a.py"},
            {"op": "edit", "path": "b.py"},
        ]
    }


def test_bad_table_name_is_still_rejected() -> None:
    import pytest

    with pytest.raises(ValueError) as excinfo:
        supertool._mini_toml_loads("[not a name]\nfoo = 1\n")
    assert "bad [table] name" in str(excinfo.value)


def test_unterminated_table_header_is_reported() -> None:
    import pytest

    with pytest.raises(ValueError) as excinfo:
        supertool._mini_toml_loads("[set\nfoo = 1\n")
    assert "unterminated [table] header" in str(excinfo.value)
