"""#2308 follow-up (self-review, oss:auditor + Explore reviewers): a
`builtin-ops.<op>` entry that is not a table did not merely fall back to
the default at `_get_op_int`/`_grep_file_includes` -- it crashed the whole
op with `AttributeError`, contradicting both the new `_preset_warnings`
message and the commit's own rationale, which claim a silent default.
`_get_op_bool` already carried the `isinstance(op_cfg, dict)` guard these
two lacked (`_supertool.py:1937` at the time of writing); this pins the
missing half of that same guard.

Would this test fail if the code did nothing? Yes -- at f6179b8f (this
lane's own #2308 commit before this follow-up), both crash with
`AttributeError: 'str' object has no attribute 'get'`.
"""
from __future__ import annotations

import supertool


def _set_builtin_ops(monkeypatch, entry):
    monkeypatch.setattr(supertool, "_CONFIG", {"builtin-ops": entry})
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    monkeypatch.setattr(supertool, "_CONFIG_PATH", None)


def test_get_op_int_falls_back_instead_of_crashing_on_a_non_table_entry(
        monkeypatch):
    _set_builtin_ops(monkeypatch, {"read": "not-a-dict-oops"})

    assert supertool._get_op_int("read", "max_lines", 100) == 100


def test_get_op_int_still_reads_a_well_formed_entry(monkeypatch):
    """Must-fire's pair: a genuinely well-shaped entry must keep working."""
    _set_builtin_ops(monkeypatch, {"read": {"max_lines": 3}})

    assert supertool._get_op_int("read", "max_lines", 100) == 3


def test_grep_file_includes_falls_back_instead_of_crashing_on_a_non_table_entry(
        monkeypatch):
    monkeypatch.setattr(supertool, "_GREP_EXTENSIONS_EFFECTIVE", None)
    _set_builtin_ops(monkeypatch, {"grep": "not-a-dict-oops"})

    assert supertool._grep_file_includes() is None


def test_grep_file_includes_still_reads_a_well_formed_entry(monkeypatch):
    monkeypatch.setattr(supertool, "_GREP_EXTENSIONS_EFFECTIVE", None)
    _set_builtin_ops(monkeypatch, {"grep": {"extensions": ["*.py"]}})

    assert supertool._grep_file_includes() == ("*.py",)
