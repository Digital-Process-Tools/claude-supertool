"""Tests for #790 — .cs mapped to a grammar name the installed tree-sitter
package does not recognise, silently swallowed to an empty symbol list.

Covers three things:
  1. The C# name mismatch itself (real extraction on a real .cs fixture).
  2. Every other _TS_LANG_MAP entry actually resolves under whichever
     package is installed here — a permanent regression guard for the
     "is C# the only one" question the issue asks.
  3. A grammar that fails to load is distinguishable from a file that
     genuinely has no definitions (the three-state contract), for both
     map: and between:.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import supertool
from conftest import _has_any_tree_sitter

_CSHARP_FIXTURE = """\
using System;

namespace Demo
{
    public class Greeter
    {
        public string Name { get; set; }

        public void SayHello()
        {
            Console.WriteLine("hello " + Name);
        }
    }
}
"""


@pytest.fixture(autouse=True)
def _reset_ts_state():
    """Tree-sitter detection and the grammar-failure cache are cached at
    module scope; tests that poke either must not leak into the next test."""
    checked, available, package = (
        supertool._TS_CHECKED, supertool._TS_AVAILABLE, supertool._TS_PACKAGE,
    )
    failed = dict(supertool._TS_GRAMMAR_FAILED)
    yield
    supertool._TS_CHECKED, supertool._TS_AVAILABLE, supertool._TS_PACKAGE = (
        checked, available, package,
    )
    supertool._TS_GRAMMAR_FAILED.clear()
    supertool._TS_GRAMMAR_FAILED.update(failed)


# ---------------------------------------------------------------------------
# 1. C# actually yields symbols (the concrete bug)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _has_any_tree_sitter(), reason="tree-sitter not installed")
def test_csharp_ts_extract_finds_symbols(tmp_path: Path, enable_tree_sitter) -> None:
    """_ts_extract on a real .cs file must not come back empty (#790).

    Would pass trivially if the code did nothing only if _ts_extract's
    contract were 'return anything' — it isn't: this asserts an actual
    class and method are found, by name.
    """
    f = tmp_path / "Greeter.cs"
    f.write_text(_CSHARP_FIXTURE)

    supertool._has_tree_sitter()  # populate _TS_PACKAGE — _ts_extract itself doesn't
    lang_name = supertool._TS_LANG_MAP[".cs"]
    symbols = supertool._ts_extract(str(f), lang_name)

    names = {name for (_kind, name, *_rest) in symbols}
    assert "Greeter" in names
    assert "SayHello" in names


@pytest.mark.skipif(not _has_any_tree_sitter(), reason="tree-sitter not installed")
def test_csharp_op_map_reports_tree_sitter_tier(tmp_path: Path, enable_tree_sitter) -> None:
    """map: on a .cs file should extract via tree-sitter, not fall through
    to ctags/regex and definitely not report '(no symbols)'."""
    f = tmp_path / "Greeter.cs"
    f.write_text(_CSHARP_FIXTURE)

    out = supertool.op_map(str(f))

    assert "(no symbols)" not in out
    assert "Greeter" in out
    assert "SayHello" in out


@pytest.mark.skipif(not _has_any_tree_sitter(), reason="tree-sitter not installed")
def test_csharp_between_symbol_finds_method(tmp_path: Path, enable_tree_sitter) -> None:
    """between: on a .cs file should locate a named method body, not
    report 'symbol not found' — the current, wrong, ambiguous failure."""
    f = tmp_path / "Greeter.cs"
    f.write_text(_CSHARP_FIXTURE)

    out = supertool.op_between_symbol("SayHello", str(f))

    assert "not found" not in out
    assert "Console.WriteLine" in out


# ---------------------------------------------------------------------------
# 2. Is C# the only mismatched entry?
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _has_any_tree_sitter(), reason="tree-sitter not installed")
def test_every_ts_lang_map_entry_resolves(enable_tree_sitter) -> None:
    """Every _TS_LANG_MAP value must resolve to a real parser under
    whichever tree-sitter package is actually installed here.

    This is the permanent form of the "cheap loop" the issue asks for:
    a future added extension with a mismatched name fails CI here instead
    of shipping as a silent zero.
    """
    supertool._has_tree_sitter()  # populate _TS_PACKAGE
    unresolved = []
    for ext, lang_name in supertool._TS_LANG_MAP.items():
        try:
            supertool._ts_get_parser(lang_name)
        except LookupError as e:
            unresolved.append((ext, lang_name, str(e)))
    assert unresolved == []


# ---------------------------------------------------------------------------
# 3. A failed-to-load grammar is distinguishable from "no definitions"
# ---------------------------------------------------------------------------

def test_ts_get_parser_tries_alias_before_failing() -> None:
    """_ts_get_parser must retry the other package's spelling before
    giving up — this is the 'try-both' the issue asks for, not a bare
    string swap that only fixes one installed package."""
    supertool._TS_CHECKED = True
    supertool._TS_AVAILABLE = True
    supertool._TS_PACKAGE = "pack"
    supertool._TS_GRAMMAR_FAILED.clear()

    calls = []

    def fake_get_parser(name):
        calls.append(name)
        if name == "c_sharp":
            raise LookupError(f"Language not found: {name}")
        return "PARSER-FOR-" + name

    fake_module = type("M", (), {"get_parser": staticmethod(fake_get_parser)})
    with patch.dict("sys.modules", {"tree_sitter_language_pack": fake_module}):
        result = supertool._ts_get_parser("c_sharp")

    assert result == "PARSER-FOR-csharp"
    assert calls == ["c_sharp", "csharp"]


def test_ts_get_parser_raises_lookup_error_when_both_names_fail() -> None:
    """When neither spelling resolves, _ts_get_parser raises LookupError
    (not some other exception) and records the failure for callers to
    report distinctly from 'ran fine, found nothing' (#790)."""
    supertool._TS_CHECKED = True
    supertool._TS_AVAILABLE = True
    supertool._TS_PACKAGE = "pack"
    supertool._TS_GRAMMAR_FAILED.clear()

    def fake_get_parser(name):
        raise LookupError(f"Language not found: {name}")

    fake_module = type("M", (), {"get_parser": staticmethod(fake_get_parser)})
    with patch.dict("sys.modules", {"tree_sitter_language_pack": fake_module}):
        with pytest.raises(LookupError):
            supertool._ts_get_parser("nonexistent_lang")

    assert "nonexistent_lang" in supertool._TS_GRAMMAR_FAILED


def test_ts_extract_returns_empty_on_grammar_failure(
    tmp_path: Path,
) -> None:
    """A grammar-load failure still returns [] from _ts_extract (existing
    contract, tiers below it still get a chance) — the LookupError does
    not propagate out and crash the caller."""
    f = tmp_path / "whatever.zz"
    f.write_text("content\n")

    supertool._TS_CHECKED = True
    supertool._TS_AVAILABLE = True
    supertool._TS_PACKAGE = "pack"
    supertool._TS_GRAMMAR_FAILED.clear()

    with patch.object(
        supertool, "_ts_get_parser", side_effect=LookupError("no such grammar")
    ):
        symbols = supertool._ts_extract(str(f), "totally_bogus_lang")

    assert symbols == []


def test_ts_get_parser_populates_grammar_failed_cache_on_real_failure() -> None:
    """The population half of the contract, exercised without mocking
    _ts_get_parser itself (that would just assert the mock did what the
    mock was told to do) — patch the underlying get_parser instead, same
    as test_ts_get_parser_tries_alias_before_failing."""
    supertool._TS_CHECKED = True
    supertool._TS_AVAILABLE = True
    supertool._TS_PACKAGE = "pack"
    supertool._TS_GRAMMAR_FAILED.clear()

    def fake_get_parser(name):
        raise LookupError(f"Language not found: {name}")

    fake_module = type("M", (), {"get_parser": staticmethod(fake_get_parser)})
    with patch.dict("sys.modules", {"tree_sitter_language_pack": fake_module}):
        symbols = supertool._ts_extract("irrelevant.zz", "totally_bogus_lang")

    assert symbols == []
    assert "totally_bogus_lang" in supertool._TS_GRAMMAR_FAILED


def test_op_map_notes_grammar_unavailable_rather_than_bare_no_symbols(
    tmp_path: Path,
) -> None:
    """When tree-sitter is 'available' (package importable) but the
    specific grammar can't be loaded, and no other tier finds anything
    either, op_map's output must say so — not render byte-identical to a
    file that genuinely has zero definitions."""
    f = tmp_path / "example.py"
    f.write_text("x = 1\n")  # no defs for ctags/regex to find either

    supertool._TS_CHECKED = True
    supertool._TS_AVAILABLE = True
    supertool._TS_PACKAGE = "pack"
    supertool._TS_GRAMMAR_FAILED.clear()
    supertool._TS_GRAMMAR_FAILED["python"] = "Language not found: python"

    with patch.object(supertool, "_has_tree_sitter", return_value=True), \
         patch.object(supertool, "_has_ctags", return_value=False), \
         patch.object(
             supertool, "_ts_get_parser",
             side_effect=LookupError("Language not found: python"),
         ):
        out = supertool.op_map(str(f))

    assert "(no symbols)" not in out
    assert "grammar" in out.lower()


def test_op_between_symbol_names_grammar_failure_not_symbol_not_found(
    tmp_path: Path,
) -> None:
    """between: on a file whose grammar failed to load must say the
    grammar is unavailable, not the misleading 'symbol not found' —
    the latter reads as 'you searched wrong', not 'we couldn't search'."""
    f = tmp_path / "example.py"
    f.write_text("def foo():\n    pass\n")

    supertool._TS_CHECKED = True
    supertool._TS_AVAILABLE = True
    supertool._TS_PACKAGE = "pack"
    supertool._TS_GRAMMAR_FAILED.clear()
    supertool._TS_GRAMMAR_FAILED["python"] = "Language not found: python"

    with patch.object(supertool, "_has_tree_sitter", return_value=True), \
         patch.object(
             supertool, "_ts_get_parser",
             side_effect=LookupError("Language not found: python"),
         ):
        out = supertool.op_between_symbol("foo", str(f))

    assert "grammar" in out.lower()
    assert out.strip().startswith("ERROR:")
