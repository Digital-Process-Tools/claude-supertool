"""#2157 -- `_heading_paths`' setext detection reads only the single line
immediately above the underline. CommonMark allows a setext heading's title
to span multiple consecutive lines of text, and #1123 (which first taught
`_heading_paths` to see setext at all) only ever looked one line back.

The failure this misses: the SAME heading, conceptually, written once with a
genuinely multi-line setext title and once as a single physical line (ATX,
or a one-line setext) carrying the full text. Reading only the last line of
a multi-line title means the two occurrences hash to different keys --
"A heading whose text runs across two lines" vs just "runs across two lines"
-- so the union's real duplicate goes unseen. That is strictly worse than
missing a heading style entirely: the guard runs, finds nothing, and reports
`markers: clean` over a document that will reparent everything under the
first copy once staged.

Must-fire / must-not-fire in the same fixture, per the issue: a genuine
multi-line-titled duplicate must be caught, and none of the existing
setext guards (thematic break, single-use heading, the single-`-`
list-item ambiguity) may be broken by widening the lookback -- re-verified
here rather than assumed still covered by #1123's own tests, since a wider
walk-back gives each guard more text to misread.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

PRESET = Path(__file__).parent.parent / "presets" / "git" / "resolve.py"
_spec = importlib.util.spec_from_file_location("git_resolve_2157", PRESET)
assert _spec is not None and _spec.loader is not None
resolve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(resolve)


def _write(tmp_path: Path, text: str) -> Path:
    f = tmp_path / "CHANGELOG.md"
    f.write_text(text, encoding="utf-8")
    return f


# The same conceptual heading, "A heading whose text runs across two lines",
# written once as a single-physical-line setext title before the hunk
# (untagged) and once as a genuine two-line setext title inside the hunk
# (tagged) -- the same underline style both times, differing only in how the
# title text is wrapped across physical lines. Reading only the underline's
# immediately-preceding line gives the two-line copy the title "runs across
# two lines" -- missing "A heading whose text" -- so the two copies never
# compare equal under the old, single-line logic, and the real duplicate
# slips past.
MULTILINE_TITLE_DUP = (
    "Changelog\n"
    "=========\n"
    "\n"
    "A heading whose text runs across two lines\n"
    "--------------------------------------------\n"
    "\n"
    "- context: something before\n"
    "\n"
    "<<<<<<< HEAD\n"
    "Changed\n"
    "-------\n"
    "\n"
    "- ours: changed a thing\n"
    "=======\n"
    "A heading whose text\n"
    "runs across two lines\n"
    "---------------------\n"
    "\n"
    "- theirs: fixed a thing\n"
    ">>>>>>> branch\n"
)

# A blank line breaks a paragraph -- CommonMark rule, unrelated to how far
# back the lookback walks. Two lines of ordinary text, then a blank line,
# then a bare `---`: the blank must still stop the walk-back cold, so the
# `---` reads as a thematic break, not a heading titled from the lines two
# rows above the blank.
MULTILINE_BLANK_BREAKS_LOOKBACK = (
    "Changelog\n"
    "=========\n"
    "\n"
    "Line one of the paragraph\n"
    "line two of the paragraph\n"
    "\n"
    "---\n"
    "\n"
    "<<<<<<< HEAD\n"
    "Changed\n"
    "-------\n"
    "\n"
    "- ours: changed a thing\n"
    "=======\n"
    "Fixed\n"
    "-----\n"
    "\n"
    "- theirs: fixed a thing\n"
    ">>>>>>> branch\n"
)

# A genuine multi-line setext title, used exactly once. The ordinary case a
# wider lookback must stay out of the way of.
MULTILINE_SETEXT_SINGLE_USE = (
    "Changelog\n"
    "=========\n"
    "\n"
    "A heading whose text\n"
    "runs across two lines\n"
    "---------------------\n"
    "\n"
    "<<<<<<< HEAD\n"
    "- ours: fixed the marker gate\n"
    "=======\n"
    "- theirs: fixed the digest\n"
    ">>>>>>> branch\n"
)

# A two-line paragraph immediately followed by a bare single `-` -- still
# ambiguous with an empty list item, still must not be read as an underline,
# even though the lookback now has two lines of candidate title text sitting
# above it instead of one. A false heading here would push "Line one... line
# two..." onto the stack and reparent the real ATX duplicate below it, hiding
# the exact thing this guard exists to catch.
BARE_DASH_AFTER_MULTILINE_PARAGRAPH = (
    "## Unreleased\n"
    "\n"
    "<<<<<<< HEAD\n"
    "### Fixed\n"
    "\n"
    "- ours: fixed the marker gate\n"
    "=======\n"
    "Some heading\n"
    "text spanning two lines\n"
    "-\n"
    "\n"
    "### Fixed\n"
    "\n"
    "- theirs: fixed the digest\n"
    ">>>>>>> branch\n"
)


def test_a_multiline_setext_title_duplicate_is_caught(tmp_path: Path) -> None:
    """MUST FIRE. #2157's own reported gap: the same heading recognised under
    two different names because only the underline's immediate predecessor
    was read as title text."""
    f = _write(tmp_path, MULTILINE_TITLE_DUP)
    dups = resolve._duplicated_headings(str(f))
    assert dups, "a multi-line setext title duplicated across the union went unseen"
    assert any("heading whose text runs across two lines" in d for d in dups)


def test_a_blank_line_still_breaks_the_lookback(tmp_path: Path) -> None:
    """MUST NOT FIRE. A blank line ends the paragraph regardless of how far
    the lookback can walk; the `---` below it is a thematic break."""
    f = _write(tmp_path, MULTILINE_BLANK_BREAKS_LOOKBACK)
    dups = resolve._duplicated_headings(str(f))
    assert dups == [], f"a thematic break was misread as a heading: {dups}"


def test_a_multiline_setext_heading_used_once_unions_quietly(tmp_path: Path) -> None:
    """MUST NOT FIRE. The ordinary case: one multi-line setext heading, used
    once."""
    f = _write(tmp_path, MULTILINE_SETEXT_SINGLE_USE)
    dups = resolve._duplicated_headings(str(f))
    assert dups == []


def test_a_bare_dash_after_a_multiline_paragraph_is_never_a_heading(tmp_path: Path) -> None:
    """MUST FIRE (on the real duplicate), MUST NOT fabricate a heading from
    the two-line paragraph above the bare `-`. A false ancestor there would
    reparent the second `### Fixed` and hide the genuine cross-side
    duplicate."""
    f = _write(tmp_path, BARE_DASH_AFTER_MULTILINE_PARAGRAPH)
    dups = resolve._duplicated_headings(str(f))
    assert dups, "a bare '-' after a two-line paragraph swallowed it as a false heading and hid the real duplicate"
    assert any("Fixed" in d for d in dups)
