"""Tests for #887 — map rendered "(no symbols)" for a file type no tier can
parse, stating a missing parser as a missing structure.

Two halves, and the first is the class fix:

  1. Three states, not two. A file type no tier parses must say so
     ("no symbol parser for .ext") and must stay distinguishable from a
     parsed file that genuinely holds zero definitions, which keeps saying
     "(no symbols)". Trading the loud failure for the quiet one would be
     the same bug wearing new words.
  2. Markdown gets a real parser. `.md` maps to the tree-sitter `markdown`
     grammar that `tree_sitter_language_pack` already ships, so headings
     render as a nested symbol tree.

Everything here asserts on the rendered `op_map` output, not on an
extractor returning a list — the defect was a rendering that misstated a
fact, so the render is what has to be pinned.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import supertool
from conftest import _has_any_tree_sitter


def _has_markdown_grammar() -> bool:
    """The markdown grammar ships with tree_sitter_language_pack but not with
    the older tree_sitter_languages, so tree-sitter being installed is not the
    same question as markdown being parseable here."""
    if not _has_any_tree_sitter():
        return False
    checked, available, package = (
        supertool._TS_CHECKED, supertool._TS_AVAILABLE, supertool._TS_PACKAGE)
    try:
        supertool._TS_CHECKED = False
        supertool._has_tree_sitter()
        supertool._ts_get_parser("markdown")
        return True
    except Exception:
        return False
    finally:
        supertool._TS_CHECKED = checked
        supertool._TS_AVAILABLE = available
        supertool._TS_PACKAGE = package
        supertool._TS_GRAMMAR_FAILED.pop("markdown", None)


_needs_markdown = pytest.mark.skipif(
    not _has_markdown_grammar(),
    reason="no tree-sitter markdown grammar in this environment")

_MD_FIXTURE = """\
# Changelog

Some prose that is not a heading.

## [Unreleased]

### Added

- a thing

## [0.25.0] - 2026-08-01

### Fixed

- another thing
"""


# ---------------------------------------------------------------------------
# 1. Three states: parser missing vs. symbols missing
# ---------------------------------------------------------------------------

def test_map_unparsed_extension_discloses_missing_parser(tmp_path: Path) -> None:
    """A .rst file: no tier has patterns for it, so map must say the parser
    is missing rather than report the document as structureless."""
    f = tmp_path / "guide.rst"
    f.write_text("Title\n=====\n\nSection\n-------\n")
    out = supertool.op_map(str(f))
    assert "no symbol parser" in out
    assert "(no symbols)" not in out


def test_map_unparsed_extension_names_the_extension(tmp_path: Path) -> None:
    """The disclosure has to name the extension it declined on — a bare
    'unsupported' leaves the reader guessing which of their files it meant."""
    f = tmp_path / "notes.rst"
    f.write_text("Title\n=====\n")
    out = supertool.op_map(str(f))
    assert "no symbol parser for .rst" in out


def test_map_genuine_no_symbols_still_reads_as_no_symbols(tmp_path: Path) -> None:
    """The loud failure must not be traded for the quiet one: Python parses
    fine, this file simply defines nothing, and that stays '(no symbols)'."""
    f = tmp_path / "script.py"
    f.write_text("x = 1\ny = 2\nprint(x + y)\n")
    out = supertool.op_map(str(f))
    assert "(no symbols)" in out
    assert "no symbol parser" not in out


def test_map_two_absences_render_differently(tmp_path: Path) -> None:
    """The whole point of the issue, as one assertion: the two facts must
    not produce the same bytes."""
    parsed = tmp_path / "empty.py"
    parsed.write_text("x = 1\n")
    unparsed = tmp_path / "empty.rst"
    unparsed.write_text("just prose\n")
    body_parsed = supertool.op_map(str(parsed)).split("\n", 1)[1]
    body_unparsed = supertool.op_map(str(unparsed)).split("\n", 1)[1]
    assert body_parsed.split("(", 1)[-1] != body_unparsed.split("(", 1)[-1]


def test_map_unparsed_extension_reports_tier_none(tmp_path: Path) -> None:
    """The report line must not claim the regex tier ran on a file the regex
    tier has no patterns for."""
    f = tmp_path / "data.csv"
    f.write_text("a,b,c\n1,2,3\n")
    out = supertool.op_map(str(f))
    assert "tier: regex" not in out


def test_map_disclosure_says_tree_sitter_is_absent_when_it_is(
        tmp_path: Path) -> None:
    """Without tree-sitter the reason is different in a way the reader can
    act on: installing a package might fix it."""
    f = tmp_path / "guide.rst"
    f.write_text("Title\n=====\n")
    out = supertool.op_map(str(f))
    assert "tree-sitter is not installed" in out


def test_map_disclosure_names_the_tiers_that_were_available(
        tmp_path: Path, enable_tree_sitter: None) -> None:
    """With tree-sitter present the disclosure must say so — a reader who
    installs the package and sees the same sentence learns nothing."""
    f = tmp_path / "guide.rst"
    f.write_text("Title\n=====\n")
    out = supertool.op_map(str(f))
    assert "no symbol parser for .rst" in out
    assert "tree-sitter and the regex tier have no .rst grammar" in out


def test_map_mixed_directory_keeps_the_parsed_tier_label(
        tmp_path: Path) -> None:
    """tier: none is only honest when nothing in the run was parseable; one
    unparsed file among parsed ones must not relabel the whole report."""
    (tmp_path / "a.py").write_text("class Alpha:\n    pass\n")
    (tmp_path / "b.rst").write_text("Title\n=====\n")
    out = supertool.op_map(str(tmp_path))
    assert "tier: none" not in out


# ---------------------------------------------------------------------------
# 2. Markdown parser
# ---------------------------------------------------------------------------

@_needs_markdown
def test_map_markdown_extracts_headings(
        tmp_path: Path, enable_tree_sitter: None) -> None:
    f = tmp_path / "CHANGELOG.md"
    f.write_text(_MD_FIXTURE)
    out = supertool.op_map(str(f))
    assert "(no symbols)" not in out
    assert "h1 Changelog" in out
    assert "h2 [Unreleased]" in out
    assert "h3 Added" in out
    assert "h2 [0.25.0] - 2026-08-01" in out


@_needs_markdown
def test_map_markdown_reports_line_numbers(
        tmp_path: Path, enable_tree_sitter: None) -> None:
    """The heading map's job is telling the caller where a section starts —
    a name without a line number would not have prevented #887's wrong count."""
    f = tmp_path / "CHANGELOG.md"
    f.write_text(_MD_FIXTURE)
    out = supertool.op_map(str(f))
    line = next(ln for ln in out.split("\n") if "h2 [Unreleased]" in ln)
    assert "[5]" in line


@_needs_markdown
def test_map_markdown_nests_by_heading_level(
        tmp_path: Path, enable_tree_sitter: None) -> None:
    f = tmp_path / "doc.md"
    f.write_text("# Top\n\n## Mid\n\n### Deep\n")
    out = supertool.op_map(str(f))
    lines = {ln.strip().split("  ")[0]: ln
             for ln in out.split("\n") if ln.strip().startswith("h")}
    indent = {k: len(v) - len(v.lstrip(" ")) for k, v in lines.items()}
    assert indent["h1 Top"] < indent["h2 Mid"] < indent["h3 Deep"]


@_needs_markdown
def test_map_markdown_setext_headings(
        tmp_path: Path, enable_tree_sitter: None) -> None:
    f = tmp_path / "doc.md"
    f.write_text("Title\n=====\n\nSection\n-------\n")
    out = supertool.op_map(str(f))
    assert "h1 Title" in out
    assert "h2 Section" in out


@_needs_markdown
def test_map_markdown_ignores_prose_and_code_fences(
        tmp_path: Path, enable_tree_sitter: None) -> None:
    """A '#' inside a fenced block is a comment, not a heading — the reason
    this is a grammar entry and not another hand-written regex."""
    f = tmp_path / "doc.md"
    f.write_text("# Real\n\n```python\n# not a heading\n```\n\nplain text\n")
    out = supertool.op_map(str(f))
    assert "h1 Real" in out
    assert "not a heading" not in out


@_needs_markdown
def test_map_markdown_file_with_no_headings_says_no_symbols(
        tmp_path: Path, enable_tree_sitter: None) -> None:
    """Markdown now has a parser, so a heading-free markdown file is a
    genuine zero — the '(no symbols)' state, not the disclosure."""
    f = tmp_path / "prose.md"
    f.write_text("just some prose\n\nand more prose\n")
    out = supertool.op_map(str(f))
    assert "(no symbols)" in out
    assert "no symbol parser" not in out


@_needs_markdown
def test_map_directory_includes_markdown(
        tmp_path: Path, enable_tree_sitter: None) -> None:
    (tmp_path / "a.py").write_text("class Alpha:\n    pass\n")
    (tmp_path / "README.md").write_text("# Readme\n")
    out = supertool.op_map(str(tmp_path))
    assert "2 files" in out
    assert "h1 Readme" in out


@_needs_markdown
def test_map_markdown_tier_is_tree_sitter(
        tmp_path: Path, enable_tree_sitter: None) -> None:
    """Headings come from the grammar, not from a fourth hand-written pattern
    set — the report line is where that claim is visible."""
    f = tmp_path / "doc.md"
    f.write_text("# Top\n")
    out = supertool.op_map(str(f))
    assert "tier: tree-sitter" in out


# ---------------------------------------------------------------------------
# 3. read: abstract mode must not swallow prose
# ---------------------------------------------------------------------------

@_needs_markdown
def test_read_of_markdown_returns_prose_not_the_heading_map(
        tmp_path: Path, enable_tree_sitter: None) -> None:
    """Giving .md a grammar must not enrol markdown in read:'s abstract mode.
    A signature list stands in for a function body; a heading list does not
    stand in for the prose under it."""
    f = tmp_path / "BIG.md"
    f.write_text("# Heading\n\n" + ("prose line with real content\n" * 400))
    out = supertool.op_read(str(f))
    assert "prose line with real content" in out


@_needs_markdown
def test_read_abstract_still_applies_to_code(
        tmp_path: Path, enable_tree_sitter: None) -> None:
    """The markdown exemption must be an exemption, not a switch that turned
    abstract reads off for everyone."""
    assert supertool._abstract_lang("x.py") == "python"
    assert supertool._abstract_lang("x.md") == ""
