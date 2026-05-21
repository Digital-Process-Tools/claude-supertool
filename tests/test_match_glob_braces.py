"""TDD: brace expansion in match glob (`*.{a,b,c}`).

The validator/formatter framework reads `spec["match"]` (e.g.
`*.{xml,scss,css,js,json,yml,yaml,md}`) and feeds it to `fnmatch.fnmatch`,
which doesn't expand braces — so prettier silently never fired on DVSI.

These tests cover the helper that expands brace patterns before matching.
"""
from __future__ import annotations

import supertool


def test_brace_pattern_matches_any_alternative() -> None:
    assert supertool._match_glob("test.json", "*.{xml,json,md}") is True
    assert supertool._match_glob("test.xml", "*.{xml,json,md}") is True
    assert supertool._match_glob("test.md", "*.{xml,json,md}") is True


def test_brace_pattern_rejects_non_matching_extension() -> None:
    assert supertool._match_glob("test.py", "*.{xml,json,md}") is False
    assert supertool._match_glob("test.txt", "*.{xml,json,md}") is False


def test_plain_pattern_still_works() -> None:
    assert supertool._match_glob("test.php", "*.php") is True
    assert supertool._match_glob("test.json", "*.php") is False


def test_dvsi_real_world_prettier_pattern() -> None:
    """The exact pattern from DVSI's .supertool.json prettier formatter."""
    pat = "*.{xml,scss,css,js,json,yml,yaml,md}"
    for ext in ("xml", "scss", "css", "js", "json", "yml", "yaml", "md"):
        assert supertool._match_glob(f"foo.{ext}", pat) is True, f"expected match for .{ext}"
    assert supertool._match_glob("foo.php", pat) is False


def test_nested_path_with_braces() -> None:
    assert supertool._match_glob("src/foo/bar.json", "*.{json,xml}") is True
    assert supertool._match_glob("src/foo/bar.txt", "*.{json,xml}") is False


def test_no_braces_passes_through_to_fnmatch() -> None:
    # Should behave identically to fnmatch for non-brace patterns
    assert supertool._match_glob("Dockerfile.dev", "Dockerfile*") is True
    assert supertool._match_glob("config.ini", "*.ini") is True


def test_empty_or_star_match_anything() -> None:
    assert supertool._match_glob("anything.xyz", "*") is True
