from __future__ import annotations

from pathlib import Path

import pytest

import supertool
from conftest import _has_any_tree_sitter


# ---------------------------------------------------------------------------
# op_between_pattern (regex line slice — no tree-sitter required)
# ---------------------------------------------------------------------------

def test_pattern_basic_slice(tmp_path: Path) -> None:
    f = tmp_path / "src.txt"
    f.write_text("alpha\nbeta\ngamma\ndelta\nepsilon\n")
    out = supertool.op_between_pattern("beta", "delta", str(f))
    assert "slice lines 2–4" in out
    assert "3 lines" in out
    assert "beta" in out
    assert "gamma" in out
    assert "delta" in out
    assert "alpha" not in out
    assert "epsilon" not in out


def test_pattern_markers_on_endpoints(tmp_path: Path) -> None:
    f = tmp_path / "src.txt"
    f.write_text("first\nMID\nlast\n")
    out = supertool.op_between_pattern("first", "last", str(f))
    # First and last lines of the slice get → marker
    assert "     1→first" in out
    assert "     3→last" in out
    # Interior lines get space
    assert "     2 MID" in out


def test_pattern_uses_regex(tmp_path: Path) -> None:
    f = tmp_path / "src.py"
    f.write_text("def foo():\n    return 1\n\ndef bar():\n    return 2\n")
    out = supertool.op_between_pattern(r"^def foo", r"^def bar", str(f))
    assert "def foo" in out
    assert "return 1" in out
    # End match is included (inclusive)
    assert "def bar" in out


def test_pattern_invalid_regex_falls_back_to_literal(tmp_path: Path) -> None:
    f = tmp_path / "src.txt"
    f.write_text("[unclosed\nbody\nend]\n")
    # '[unclosed' is invalid regex — should fall back to literal match
    out = supertool.op_between_pattern("[unclosed", "end]", str(f))
    assert "ERROR" not in out
    assert "[unclosed" in out


def test_pattern_start_not_matched(tmp_path: Path) -> None:
    f = tmp_path / "src.txt"
    f.write_text("a\nb\nc\n")
    out = supertool.op_between_pattern("XYZZY", "b", str(f))
    assert "ERROR: start pattern" in out
    assert "XYZZY" in out


def test_pattern_end_not_matched(tmp_path: Path) -> None:
    f = tmp_path / "src.txt"
    f.write_text("a\nb\nc\n")
    out = supertool.op_between_pattern("a", "XYZZY", str(f))
    assert "ERROR: end pattern" in out
    assert "XYZZY" in out


def test_pattern_end_must_be_after_start(tmp_path: Path) -> None:
    f = tmp_path / "src.txt"
    f.write_text("end_line\nmiddle\nstart_line\n")
    # 'end_line' appears at line 1, 'start_line' at line 3.
    # If we start at line 3 but want 'end_line' AFTER it, it should fail.
    out = supertool.op_between_pattern("start_line", "end_line", str(f))
    assert "ERROR: end pattern" in out


def test_pattern_empty_start_errors(tmp_path: Path) -> None:
    f = tmp_path / "src.txt"
    f.write_text("a\n")
    out = supertool.op_between_pattern("", "a", str(f))
    assert "ERROR: empty start pattern" in out


def test_pattern_empty_end_errors(tmp_path: Path) -> None:
    f = tmp_path / "src.txt"
    f.write_text("a\n")
    out = supertool.op_between_pattern("a", "", str(f))
    assert "ERROR: empty end pattern" in out


def test_pattern_missing_file(tmp_path: Path) -> None:
    out = supertool.op_between_pattern("a", "b", str(tmp_path / "nope.txt"))
    assert "ERROR: file not found" in out


def test_pattern_directory_errors(tmp_path: Path) -> None:
    out = supertool.op_between_pattern("a", "b", str(tmp_path))
    assert "ERROR" in out
    assert "directories" in out or "directory" in out


# ---------------------------------------------------------------------------
# op_between_symbol — error paths that don't need tree-sitter
# ---------------------------------------------------------------------------

def test_symbol_no_tree_sitter_returns_helpful_error(tmp_path: Path) -> None:
    """Default conftest disables tree-sitter; symbol mode should suggest re:."""
    f = tmp_path / "src.py"
    f.write_text("def foo():\n    pass\n")
    out = supertool.op_between_symbol("foo", str(f))
    assert "ERROR" in out
    assert "tree-sitter" in out
    assert "between:re:" in out


def test_symbol_empty_symbol_errors(tmp_path: Path) -> None:
    f = tmp_path / "src.py"
    f.write_text("x\n")
    out = supertool.op_between_symbol("", str(f))
    assert "ERROR: empty symbol" in out


def test_symbol_missing_file(tmp_path: Path) -> None:
    out = supertool.op_between_symbol("foo", str(tmp_path / "nope.py"))
    assert "ERROR: file not found" in out


def test_symbol_directory_errors(tmp_path: Path) -> None:
    out = supertool.op_between_symbol("foo", str(tmp_path))
    assert "ERROR" in out
    assert "directories" in out or "directory" in out


# ---------------------------------------------------------------------------
# op_between_symbol — tree-sitter integration tests (skip if not installed)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not _has_any_tree_sitter(), reason="no tree-sitter package installed"
)
def test_symbol_python_function(tmp_path: Path, enable_tree_sitter) -> None:
    f = tmp_path / "src.py"
    f.write_text(
        "def alpha():\n"
        "    return 1\n"
        "\n"
        "def beta():\n"
        "    return 2\n"
    )
    out = supertool.op_between_symbol("beta", str(f))
    assert "def 'beta'" in out
    assert "def beta()" in out
    assert "return 2" in out
    # Should not include alpha
    assert "alpha" not in out


@pytest.mark.skipif(
    not _has_any_tree_sitter(), reason="no tree-sitter package installed"
)
def test_symbol_python_class(tmp_path: Path, enable_tree_sitter) -> None:
    f = tmp_path / "src.py"
    f.write_text(
        "class Foo:\n"
        "    def method(self):\n"
        "        return 'inside'\n"
        "\n"
        "after = 1\n"
    )
    out = supertool.op_between_symbol("Foo", str(f))
    assert "class 'Foo'" in out
    assert "class Foo:" in out
    assert "def method" in out
    assert "return 'inside'" in out
    assert "after = 1" not in out


@pytest.mark.skipif(
    not _has_any_tree_sitter(), reason="no tree-sitter package installed"
)
def test_symbol_multi_match_warns(tmp_path: Path, enable_tree_sitter) -> None:
    f = tmp_path / "src.py"
    f.write_text(
        "def dup():\n"
        "    return 1\n"
        "\n"
        "def dup():\n"
        "    return 2\n"
    )
    out = supertool.op_between_symbol("dup", str(f))
    assert "2 matches (first shown)" in out
    # First match returned: should contain return 1, not return 2
    assert "return 1" in out
    assert "return 2" not in out


@pytest.mark.skipif(
    not _has_any_tree_sitter(), reason="no tree-sitter package installed"
)
def test_symbol_not_found(tmp_path: Path, enable_tree_sitter) -> None:
    f = tmp_path / "src.py"
    f.write_text("def foo():\n    pass\n")
    out = supertool.op_between_symbol("nonexistent", str(f))
    assert "ERROR: symbol 'nonexistent' not found" in out


@pytest.mark.skipif(
    not _has_any_tree_sitter(), reason="no tree-sitter package installed"
)
def test_symbol_unsupported_extension(tmp_path: Path, enable_tree_sitter) -> None:
    f = tmp_path / "src.weird"
    f.write_text("foo bar\n")
    out = supertool.op_between_symbol("foo", str(f))
    assert "ERROR" in out
    assert ".weird" in out
    assert "between:re:" in out


# ---------------------------------------------------------------------------
# Dispatch routing
# ---------------------------------------------------------------------------

def test_dispatch_pattern_mode(tmp_path: Path) -> None:
    f = tmp_path / "src.txt"
    f.write_text("start\nmiddle\nend\n")
    out = supertool.dispatch(f"between:re:start:end:{f}")
    assert out.startswith(f"--- between:re:start:end:{f} ---\n")
    assert "slice lines 1–3" in out


def test_dispatch_re_mode_too_few_args_explicit_error(tmp_path: Path) -> None:
    """`re:` is reserved — never silently falls through to symbol mode."""
    out = supertool.dispatch("between:re:onlystart")
    assert "ERROR: between:re: requires" in out


def test_dispatch_symbol_mode_falls_through_to_no_tree_sitter(
    tmp_path: Path,
) -> None:
    f = tmp_path / "src.py"
    f.write_text("def foo():\n    pass\n")
    out = supertool.dispatch(f"between:foo:{f}")
    assert out.startswith(f"--- between:foo:{f} ---\n")
    # Default conftest disables tree-sitter, so we get the helpful error.
    assert "tree-sitter" in out


def test_dispatch_symbol_with_php_double_colon(tmp_path: Path) -> None:
    """Foo::bar should join into a single symbol, not split into pattern args."""
    f = tmp_path / "src.php"
    f.write_text("<?php\n")
    out = supertool.dispatch(f"between:Foo::bar:{f}")
    # Symbol mode reaches the tree-sitter check (means parsing kept Foo::bar
    # together rather than treating as pattern mode).
    assert "tree-sitter" in out


def test_dispatch_too_few_args(tmp_path: Path) -> None:
    out = supertool.dispatch("between:onlyone")
    assert "ERROR: between requires" in out


def test_dispatch_unknown_op_lists_between(tmp_path: Path) -> None:
    out = supertool.dispatch("nonexistentop:foo")
    assert "between" in out
