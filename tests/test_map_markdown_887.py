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


@pytest.mark.parametrize("ts_installed, expected", [
    (True, "tree-sitter and the regex tier have no .rst grammar"),
    (False, "tree-sitter is not installed and the regex tier has no .rst patterns"),
])
def test_map_disclosure_wording_tracks_tree_sitter_presence(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        ts_installed: bool, expected: str) -> None:
    """Two different facts, two different sentences. "Not installed" is
    actionable by the reader — install a package and the answer changes —
    and "no grammar for this type" is not. Collapsing them into one string
    would buy a green by deleting a real signal.

    tree-sitter presence is stubbed rather than detected. The first version
    of this test called the real detector and asserted the installed wording,
    so it passed on a laptop with the package and failed on all thirteen CI
    legs without it — pinning the environment instead of the behaviour. The
    stub is honest here because this path consults nothing but the boolean:
    ".rst" is absent from _TS_LANG_MAP, so no grammar is ever loaded, and
    test_map_disclosure_distinguishes_the_two_reasons proves the boolean is
    read rather than assumed.
    """
    monkeypatch.setattr(supertool, "_TS_CHECKED", True)
    monkeypatch.setattr(supertool, "_TS_AVAILABLE", ts_installed)
    f = tmp_path / "guide.rst"
    f.write_text("Title\n=====\n")
    out = supertool.op_map(str(f))
    assert expected in out


def test_map_disclosure_distinguishes_the_two_reasons(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same file, same extension, one variable — the renders must differ.
    Without this, a disclosure that ignored use_ts entirely could satisfy
    one half of the parametrised test above and never be caught."""
    f = tmp_path / "guide.rst"
    f.write_text("Title\n=====\n")
    monkeypatch.setattr(supertool, "_TS_CHECKED", True)

    monkeypatch.setattr(supertool, "_TS_AVAILABLE", True)
    with_ts = supertool.op_map(str(f))
    monkeypatch.setattr(supertool, "_TS_AVAILABLE", False)
    without_ts = supertool.op_map(str(f))

    assert with_ts != without_ts


@pytest.mark.parametrize("ts_installed", [True, False])
def test_map_disclosure_invariant_holds_in_every_environment(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        ts_installed: bool) -> None:
    """What must be true whatever is installed: the render names the
    extension nothing could parse, and never claims the file has no
    symbols. This is the part of #887 that is not environment-dependent."""
    monkeypatch.setattr(supertool, "_TS_CHECKED", True)
    monkeypatch.setattr(supertool, "_TS_AVAILABLE", ts_installed)
    f = tmp_path / "guide.rst"
    f.write_text("Title\n=====\n")
    out = supertool.op_map(str(f))
    assert "no symbol parser for .rst" in out
    assert "(no symbols)" not in out
    assert "tier: none" in out


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


def test_read_abstract_still_applies_to_code() -> None:
    """The markdown exemption must be an exemption, not a switch that turned
    abstract reads off for everyone.

    Deliberately ungated: it reads the maps, never the grammar, so unlike the
    tests above it runs on CI — where tree_sitter_language_pack is not
    installed and every markdown test skips."""
    assert supertool._abstract_lang("x.py") == "python"
    assert supertool._abstract_lang("x.md") == ""
    assert supertool._abstract_lang("x.markdown") == ""


# ---------------------------------------------------------------------------
# 4. Wiring, checkable without the grammar
#
# CI installs no tree-sitter package, so every test in section 2 skips there
# and the markdown feature would ship on local greens alone. These assert the
# parts that are just data — the extension maps and the dispatch — so the way
# markdown is plugged in stays covered on every leg.
# ---------------------------------------------------------------------------

def test_markdown_extensions_are_registered() -> None:
    assert supertool._TS_LANG_MAP[".md"] == "markdown"
    assert supertool._TS_LANG_MAP[".markdown"] == "markdown"


def test_markdown_is_walked_in_directory_maps() -> None:
    """_MAP_EXTENSIONS is what _collect_files filters directory walks by; if
    .md fell out of it, map:docs/ would silently list nothing."""
    assert ".md" in supertool._MAP_EXTENSIONS
    assert ".markdown" in supertool._MAP_EXTENSIONS


def test_markdown_is_the_only_abstract_read_exemption() -> None:
    """A guard on the blast radius of the exemption itself: if another
    language were added here, large files in it would quietly stop being
    abstracted and nobody would see a failing test."""
    assert supertool._ABSTRACT_READ_SKIP_LANGS == frozenset({"markdown"})


def test_markdown_extensions_have_no_regex_patterns() -> None:
    """The fix is a grammar entry, not a fourth hand-written pattern set —
    #887 rejected regex headings because a hand-rolled version counts '#'
    inside fenced code blocks. This fails if someone adds them later."""
    assert ".md" not in supertool._REGEX_PATTERNS
    assert ".markdown" not in supertool._REGEX_PATTERNS


# ---------------------------------------------------------------------------
# 5. The heading walker, exercised without the grammar
#
# Sections 2 and 3 skip entirely on CI, which is not merely a coverage gap: it
# means the walker in `_ts_extract_markdown` — the actual new logic — executes
# nowhere the project measures. The floor in `.github/scripts/coverage_gate.py`
# is what turned that from invisible into red, and the honest answer is to test
# the thing rather than exempt it.
#
# `_ts_extract_markdown` takes (source, tree) and touches only `.root_node`,
# `.type`, `.children`, `.start_point` and `.start_byte`/`.end_byte`, so a
# replay object satisfies it. The danger with any hand-built tree is that it
# asserts the author's idea of the grammar instead of the grammar — so this one
# is not hand-built. It is a recording of what tree-sitter actually emitted for
# `_MD_TREE_SOURCE`, and two guards below re-derive it from the real parser
# wherever the grammar exists. If the recording ever stops matching, they fail
# rather than letting the replay drift quietly into fiction.
# ---------------------------------------------------------------------------

_MD_TREE_SOURCE = (
    b"# Changelog\n\nSome prose that is not a heading.\n\n## [Unreleased]\n\n"
    b"### Added\n\n```python\n# not a heading\n```\n\nSetext Title\n============\n\n"
    b"Setext Sub\n----------\n\n####### seven hashes is not a heading\n"
)

#: (type, start_row, end_row, start_byte, end_byte, children) — recorded from
#: tree_sitter_language_pack's `markdown` grammar, not composed by hand.
_RECORDED_MD_TREE = (
    ("document", 0, 19, 0, 195, (
        ("section", 0, 19, 0, 195, (
            ("atx_heading", 0, 1, 0, 12, (
                ("atx_h1_marker", 0, 0, 0, 1, ()),
                ("inline", 0, 0, 2, 11, ()),
            )),
            ("paragraph", 2, 3, 13, 47, (
                ("inline", 2, 2, 13, 46, (
                    (".", 2, 2, 45, 46, ()),
                )),
            )),
            ("section", 4, 19, 48, 195, (
                ("atx_heading", 4, 5, 48, 64, (
                    ("atx_h2_marker", 4, 4, 48, 50, ()),
                    ("inline", 4, 4, 51, 63, (
                        ("[", 4, 4, 51, 52, ()),
                        ("]", 4, 4, 62, 63, ()),
                    )),
                )),
                ("section", 6, 19, 65, 195, (
                    ("atx_heading", 6, 7, 65, 75, (
                        ("atx_h3_marker", 6, 6, 65, 68, ()),
                        ("inline", 6, 6, 69, 74, ()),
                    )),
                    ("fenced_code_block", 8, 11, 76, 106, (
                        ("fenced_code_block_delimiter", 8, 8, 76, 79, ()),
                        ("info_string", 8, 8, 79, 85, (
                            ("language", 8, 8, 79, 85, ()),
                        )),
                        ("block_continuation", 9, 9, 86, 86, ()),
                        ("code_fence_content", 9, 10, 86, 102, (
                            ("#", 9, 9, 86, 87, ()),
                            ("block_continuation", 10, 10, 102, 102, ()),
                        )),
                        ("fenced_code_block_delimiter", 10, 10, 102, 105, ()),
                    )),
                    ("setext_heading", 12, 14, 107, 133, (
                        ("paragraph", 12, 13, 107, 120, (
                            ("inline", 12, 12, 107, 119, ()),
                        )),
                        ("setext_h1_underline", 13, 13, 120, 132, ()),
                    )),
                    ("setext_heading", 15, 17, 134, 156, (
                        ("paragraph", 15, 16, 134, 145, (
                            ("inline", 15, 15, 134, 144, ()),
                        )),
                        ("setext_h2_underline", 16, 16, 145, 155, ()),
                    )),
                    ("paragraph", 18, 19, 157, 195, (
                        ("inline", 18, 18, 157, 194, (
                            ("#", 18, 18, 157, 158, ()),
                            ("#", 18, 18, 158, 159, ()),
                            ("#", 18, 18, 159, 160, ()),
                            ("#", 18, 18, 160, 161, ()),
                            ("#", 18, 18, 161, 162, ()),
                            ("#", 18, 18, 162, 163, ()),
                            ("#", 18, 18, 163, 164, ()),
                        )),
                    )),
                )),
            )),
        )),
    ))
)


class _ReplayNode:
    """The subset of tree-sitter's node API `_ts_extract_markdown` consults."""

    __slots__ = ("type", "start_point", "end_point", "start_byte", "end_byte",
                 "children")

    def __init__(self, spec: tuple) -> None:
        node_type, start_row, end_row, start_byte, end_byte, children = spec
        self.type = node_type
        self.start_point = (start_row, 0)
        self.end_point = (end_row, 0)
        self.start_byte = start_byte
        self.end_byte = end_byte
        self.children = [_ReplayNode(c) for c in children]


class _ReplayTree:
    def __init__(self, spec: tuple) -> None:
        self.root_node = _ReplayNode(spec)


def _node_types(node) -> set:
    out = {node.type}
    for child in node.children:
        out |= _node_types(child)
    return out


#: What the walker must return for _MD_TREE_SOURCE: (kind, name, line, end, depth).
_EXPECTED_MD_SYMBOLS = [
    ("h1", "Changelog", 1, 1, 0),
    ("h2", "[Unreleased]", 5, 5, 1),
    ("h3", "Added", 7, 7, 2),
    ("h1", "Setext Title", 13, 13, 0),
    ("h2", "Setext Sub", 16, 16, 1),
]


def test_markdown_walker_extracts_every_heading_kind() -> None:
    """Runs on every leg, grammar or not. atx h1/h2/h3 and setext h1/h2, with
    names taken from the inline child and depth derived from the level."""
    symbols = supertool._ts_extract_markdown(
        _MD_TREE_SOURCE, _ReplayTree(_RECORDED_MD_TREE))
    assert symbols == _EXPECTED_MD_SYMBOLS


def test_markdown_walker_ignores_hashes_that_are_not_headings() -> None:
    """The `#` inside the fenced block and the seven-hash line are `#` tokens
    under `code_fence_content` and `paragraph` in the recorded tree — the
    grammar already ruled them out, and the walker must not re-admit them."""
    names = [s[1] for s in supertool._ts_extract_markdown(
        _MD_TREE_SOURCE, _ReplayTree(_RECORDED_MD_TREE))]
    assert "not a heading" not in names
    assert "seven hashes is not a heading" not in names


def test_markdown_walker_returns_symbols_in_line_order() -> None:
    lines = [s[2] for s in supertool._ts_extract_markdown(
        _MD_TREE_SOURCE, _ReplayTree(_RECORDED_MD_TREE))]
    assert lines == sorted(lines)


@_needs_markdown
def test_recorded_tree_still_matches_the_real_grammar() -> None:
    """The anti-fiction guard. Re-parses `_MD_TREE_SOURCE` with the real
    grammar and compares the whole shape against the recording, so a grammar
    upgrade that changes node types or spans fails here — loudly, on a machine
    that has the grammar — instead of leaving CI green against a fixture that
    no longer describes anything real."""
    from tree_sitter_language_pack import get_parser
    tree = get_parser("markdown").parse(_MD_TREE_SOURCE)

    def ser(node):
        return (node.type, node.start_point[0], node.end_point[0],
                node.start_byte, node.end_byte,
                tuple(ser(c) for c in node.children))

    assert ser(tree.root_node) == _RECORDED_MD_TREE


@_needs_markdown
def test_replay_and_real_grammar_extract_identically() -> None:
    """The property that actually matters, stated directly: the walker cannot
    tell the replay from the parser. Weaker than the shape comparison above and
    kept anyway — it is the one that stays meaningful if the recording is ever
    deliberately trimmed."""
    from tree_sitter_language_pack import get_parser
    real = get_parser("markdown").parse(_MD_TREE_SOURCE)
    assert (supertool._ts_extract_markdown(_MD_TREE_SOURCE, real)
            == supertool._ts_extract_markdown(
                _MD_TREE_SOURCE, _ReplayTree(_RECORDED_MD_TREE)))


@_needs_markdown
def test_replay_invents_no_node_types() -> None:
    """A recording can only shrink toward fiction by adding vocabulary the
    grammar never emits; this forbids that independently of the shape guard."""
    from tree_sitter_language_pack import get_parser
    real = get_parser("markdown").parse(_MD_TREE_SOURCE)
    assert _node_types(_ReplayNode(_RECORDED_MD_TREE)) <= _node_types(real.root_node)
