"""Brace expansion in glob op (issue #161).

`glob:src/**/*.{json,xml}` previously returned 0 files because Python's
`glob.glob` doesn't expand braces. Caller expected shell/fd/ripgrep semantics.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

import supertool


# ---------------------------------------------------------------------------
# _expand_braces helper
# ---------------------------------------------------------------------------


def test_expand_no_braces_passthrough() -> None:
    assert supertool._expand_braces("*.php") == ["*.php"]


def test_expand_single_group() -> None:
    assert supertool._expand_braces("*.{json,xml}") == ["*.json", "*.xml"]


def test_expand_three_alternatives() -> None:
    assert supertool._expand_braces("a.{x,y,z}") == ["a.x", "a.y", "a.z"]


def test_expand_multiple_groups_cartesian() -> None:
    result = supertool._expand_braces("{a,b}.{x,y}")
    assert result == ["a.x", "a.y", "b.x", "b.y"]


def test_expand_nested_groups() -> None:
    result = supertool._expand_braces("{a,b{1,2}}")
    assert result == ["a", "b1", "b2"]


def test_expand_dedupes_collisions() -> None:
    # `{a,a}` → ["a"] (not ["a", "a"])
    assert supertool._expand_braces("{a,a}") == ["a"]


def test_expand_unbalanced_returns_literal() -> None:
    # Missing close — return unchanged, let downstream handle.
    assert supertool._expand_braces("foo.{json") == ["foo.{json"]


def test_expand_empty_alternative() -> None:
    # `{,bak}` → ["", "bak"] — useful for `file{,.bak}` style
    assert supertool._expand_braces("f{,.bak}") == ["f", "f.bak"]


# ---------------------------------------------------------------------------
# op_glob integration
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a small tree with mixed extensions, chdir into it."""
    (tmp_path / "a.json").write_text("{}")
    (tmp_path / "a.xml").write_text("<x/>")
    (tmp_path / "a.txt").write_text("nope")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.json").write_text("{}")
    (sub / "b.xml").write_text("<x/>")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_op_glob_brace_returns_both_extensions(tmp_files: Path) -> None:
    out = supertool.op_glob("**/*.{json,xml}", no_auto_read=True)
    assert "a.json" in out
    assert "a.xml" in out
    assert "b.json" in out
    assert "b.xml" in out
    assert "a.txt" not in out
    # Count line says 4 files
    assert "(4 files)" in out


def test_op_glob_brace_repro_from_issue(tmp_files: Path) -> None:
    """Exact failure mode from issue #161 — should now return matches."""
    out = supertool.op_glob("**/*.{json,xml}:no-auto-read".split(":")[0], no_auto_read=True)
    assert "(0 files)" not in out


def test_op_glob_no_braces_unchanged(tmp_files: Path) -> None:
    out = supertool.op_glob("**/*.json", no_auto_read=True)
    assert "a.json" in out
    assert "b.json" in out
    assert "a.xml" not in out


def test_op_glob_brace_dedup(tmp_files: Path) -> None:
    # `{json,json}` shouldn't double-count
    out = supertool.op_glob("**/*.{json,json}", no_auto_read=True)
    assert "(2 files)" in out
