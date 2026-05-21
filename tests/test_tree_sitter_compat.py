"""Tests for tree-sitter >= 0.25 compatibility fixes."""
from __future__ import annotations

import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import supertool
from conftest import _has_any_tree_sitter


# ---------------------------------------------------------------------------
# Fix 1: _ts_parse helper — str/bytes API fallback
# ---------------------------------------------------------------------------

class _BytesOnlyParser:
    """Simulates tree-sitter <= 0.21: accepts bytes, rejects str."""
    def parse(self, src):
        if isinstance(src, bytes):
            mock_tree = MagicMock()
            mock_tree.root_node.children = []
            mock_tree.root_node.type = "module"
            return mock_tree
        raise TypeError("parse() requires bytes")


class _StrOnlyParser:
    """Simulates tree-sitter >= 0.25: accepts str, rejects bytes."""
    def parse(self, src):
        if isinstance(src, str):
            mock_tree = MagicMock()
            mock_tree.root_node.children = []
            mock_tree.root_node.type = "module"
            return mock_tree
        raise TypeError("parse() requires str, not bytes")


def test_ts_parse_bytes_accepted() -> None:
    """_ts_parse succeeds when parser accepts bytes (old API)."""
    parser = _BytesOnlyParser()
    result = supertool._ts_parse(parser, b"class Foo {}")
    assert result is not None


def test_ts_parse_str_fallback() -> None:
    """_ts_parse falls back to decoded str when parser rejects bytes (0.25+ API)."""
    parser = _StrOnlyParser()
    result = supertool._ts_parse(parser, b"class Foo {}")
    assert result is not None


def test_ts_parse_str_fallback_invalid_utf8() -> None:
    """_ts_parse handles invalid UTF-8 bytes via errors='replace'."""
    parser = _StrOnlyParser()
    result = supertool._ts_parse(parser, b"class Foo \xff\xfe {}")
    assert result is not None


# ---------------------------------------------------------------------------
# Fix 2: exception narrowing — TypeError propagates (not swallowed)
# ---------------------------------------------------------------------------

class _AlwaysTypeErrorParser:
    """Parser that raises TypeError for both str and bytes — should propagate."""
    def parse(self, src):
        raise TypeError("unsupported type")


def test_ts_parse_typeerror_on_str_propagates() -> None:
    """TypeError from str parse (not a bytes→str issue) propagates out of _ts_parse."""
    parser = _AlwaysTypeErrorParser()
    with pytest.raises(TypeError):
        supertool._ts_parse(parser, b"anything")


# ---------------------------------------------------------------------------
# Fix 3: op_map tier label reflects actual extractor
# ---------------------------------------------------------------------------

def test_op_map_tier_regex_when_ts_returns_empty(tmp_path: Path) -> None:
    """When tree-sitter is available but returns no symbols, tier should be 'regex'."""
    f = tmp_path / "example.py"
    f.write_text("x = 1\n")  # no class/function — regex won't find symbols either

    # Enable tree-sitter in supertool state but mock _ts_extract to return []
    supertool._TS_CHECKED = True
    supertool._TS_AVAILABLE = True
    supertool._TS_PACKAGE = "pack"

    with patch.object(supertool, "_ts_extract", return_value=[]), \
         patch.object(supertool, "_has_tree_sitter", return_value=True), \
         patch.object(supertool, "_has_ctags", return_value=False):
        out = supertool.op_map(str(f))

    # tree-sitter was available but produced nothing — tier must NOT be "tree-sitter"
    assert "tier: tree-sitter" not in out

    # restore
    supertool._TS_AVAILABLE = False


def test_op_map_tier_tree_sitter_when_ts_returns_symbols(tmp_path: Path) -> None:
    """When tree-sitter returns symbols, tier label should be 'tree-sitter'."""
    f = tmp_path / "example.py"
    f.write_text("class Foo:\n    pass\n")

    supertool._TS_CHECKED = True
    supertool._TS_AVAILABLE = True
    supertool._TS_PACKAGE = "pack"

    fake_symbols = [("class", "Foo", 1, 2, 0)]
    with patch.object(supertool, "_ts_extract", return_value=fake_symbols), \
         patch.object(supertool, "_has_tree_sitter", return_value=True), \
         patch.object(supertool, "_has_ctags", return_value=False):
        out = supertool.op_map(str(f))

    assert "tier: tree-sitter" in out

    # restore
    supertool._TS_AVAILABLE = False


def test_op_map_tier_ctags_when_ts_unavailable_ctags_hits(tmp_path: Path) -> None:
    """When tree-sitter is absent but ctags returns symbols, tier is 'ctags'."""
    f = tmp_path / "example.py"
    f.write_text("class Foo:\n    pass\n")

    # tree-sitter disabled (default from conftest), ctags available
    fake_ctags = [("class", "Foo", 1, None)]
    with patch.object(supertool, "_has_tree_sitter", return_value=False), \
         patch.object(supertool, "_has_ctags", return_value=True), \
         patch.object(supertool, "_ctags_extract", return_value=fake_ctags):
        out = supertool.op_map(str(f))

    assert "tier: ctags" in out


# ---------------------------------------------------------------------------
# Integration — requires real tree-sitter (skipped if not installed)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _has_any_tree_sitter(), reason="tree-sitter not installed")
def test_ts_parse_real_parser_bytes(tmp_path: Path) -> None:
    """Real tree-sitter parser works via _ts_parse (bytes path)."""
    try:
        from tree_sitter_language_pack import get_parser
    except ImportError:
        from tree_sitter_languages import get_parser

    parser = get_parser("python")
    tree = supertool._ts_parse(parser, b"class Foo:\n    pass\n")
    assert tree is not None
    assert tree.root_node is not None
