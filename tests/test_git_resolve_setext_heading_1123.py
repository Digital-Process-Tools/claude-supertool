"""#1123 -- `_heading_paths` sees `## Title` (ATX) and is blind to `Title`
underlined with `===`/`---` (setext). #911's guard reads the union's own
document to catch a duplicate heading whose twin sits outside the hunk; a
setext-styled duplicate is invisible to it in exactly the same way the bug
`_HEADING_RE` replaced was invisible to ATX before #911.

Scope, following the issue's own framing: parse setext too, rather than the
"refuse any setext document" or "swap in a real CommonMark parser" options
also named there. Parsing is the one that keeps `git-resolve both` usable on
a document that uses setext at all, and the guard's whole purpose is to
refuse a union it cannot vouch for -- refusing every setext-using document
outright would trade a silent hole for a blanket false alarm, which trains
the override exactly as a wrong `block` rule does elsewhere in this repo.

Must-fire / must-not-fire in the same fixture: a real setext duplicate must
be caught, and an ordinary thematic break (`---` with no preceding title, or
after a blank line) must not be misread as one -- CommonMark's own
distinction, and the one a naive "any line of `=`/`-` is a heading" rule
would get wrong.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

PRESET = Path(__file__).parent.parent / "presets" / "git" / "resolve.py"
_spec = importlib.util.spec_from_file_location("git_resolve_1123", PRESET)
assert _spec is not None and _spec.loader is not None
resolve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(resolve)


def _write(tmp_path: Path, text: str) -> Path:
    f = tmp_path / "CHANGELOG.md"
    f.write_text(text, encoding="utf-8")
    return f


# The #911 shape, replayed with a setext title instead of an ATX one: `Fixed`
# underlined with `---` appears once inside the hunk (theirs) and once in the
# surrounding context the union also carries.
SETEXT_DUP = (
    "Changelog\n"
    "=========\n"
    "\n"
    "Unreleased\n"
    "----------\n"
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
    "\n"
    "Added\n"
    "-----\n"
    "\n"
    "- some addition\n"
    "\n"
    "Fixed\n"
    "-----\n"
    "\n"
    "- older: fixed something else\n"
)

# A thematic break -- a bare `---` after a blank line, with no title line
# directly above it -- must not be read as a setext underline for a heading
# that is not there. Each side of the hunk defines a DIFFERENT heading here
# (unlike SETEXT_DUP above), so the only way this could be flagged is a false
# setext heading swallowing the wrong previous line as its title and
# corrupting the stack for everything after it.
THEMATIC_BREAK_ONLY = (
    "Changelog\n"
    "=========\n"
    "\n"
    "Unreleased\n"
    "----------\n"
    "\n"
    "---\n"
    "\n"
    "<<<<<<< HEAD\n"
    "Changed\n"
    "-------\n"
    "\n"
    "- ours: changed the marker gate\n"
    "=======\n"
    "Fixed\n"
    "-----\n"
    "\n"
    "- theirs: fixed the digest\n"
    ">>>>>>> branch\n"
)

# A setext title that appears exactly once must union quietly -- the ordinary
# case this guard must stay out of the way of.
SETEXT_SINGLE_USE = (
    "Changelog\n"
    "=========\n"
    "\n"
    "Unreleased\n"
    "----------\n"
    "\n"
    "Fixed\n"
    "-----\n"
    "\n"
    "<<<<<<< HEAD\n"
    "- ours: fixed the marker gate\n"
    "=======\n"
    "- theirs: fixed the digest\n"
    ">>>>>>> branch\n"
)


def test_a_duplicated_setext_heading_is_caught(tmp_path: Path) -> None:
    """MUST FIRE. #1123's own reported gap."""
    f = _write(tmp_path, SETEXT_DUP)
    dups = resolve._duplicated_headings(str(f))
    assert dups, "a setext-styled 'Fixed' duplicated across the union went unseen"
    assert any("Fixed" in d for d in dups)


def test_a_bare_thematic_break_is_not_read_as_a_heading(tmp_path: Path) -> None:
    """MUST NOT FIRE. `---` with no title line directly above it is a rule,
    not an underline -- the two `Fixed` headings here are the only real
    duplicate risk, and each is used once."""
    f = _write(tmp_path, THEMATIC_BREAK_ONLY)
    dups = resolve._duplicated_headings(str(f))
    assert dups == [], f"a thematic break was misread as a heading: {dups}"


def test_a_setext_heading_used_once_unions_quietly(tmp_path: Path) -> None:
    """MUST NOT FIRE. The ordinary case: one setext heading, used once."""
    f = _write(tmp_path, SETEXT_SINGLE_USE)
    dups = resolve._duplicated_headings(str(f))
    assert dups == []


# A single bare `-` is indistinguishable from an empty list item, and reading
# it as a heading anyway pushes a FALSE ancestor onto `_heading_paths`' stack
# -- which can silently reparent a real duplicate the ATX guard (#911) would
# already have caught, hiding it instead of merely missing a decorative
# one-character underline. Found in review before this shipped.
BARE_DASH_HIDES_A_REAL_DUPLICATE = (
    "## Unreleased\n"
    "\n"
    "<<<<<<< HEAD\n"
    "### Fixed\n"
    "\n"
    "- ours: fixed the marker gate\n"
    "=======\n"
    "Something\n"
    "-\n"
    "\n"
    "### Fixed\n"
    "\n"
    "- theirs: fixed the digest\n"
    ">>>>>>> branch\n"
)


def test_a_bare_single_dash_is_never_read_as_a_heading(tmp_path: Path) -> None:
    """MUST FIRE (on the real duplicate). A lone `-` right after a paragraph
    line must not be treated as a setext underline: doing so pushes a false
    'Something' ancestor onto the stack, reparents the second `### Fixed`
    under it, and the genuine cross-side duplicate `_heading_paths` exists
    to catch goes unseen -- worse than the bug #1123 was filed to fix, since
    now an ATX-only duplicate this guard already caught (#911) is missed too."""
    f = _write(tmp_path, BARE_DASH_HIDES_A_REAL_DUPLICATE)
    dups = resolve._duplicated_headings(str(f))
    assert dups, "a bare '-' swallowed 'Something' as a false heading and hid the real duplicate"
    assert any("Fixed" in d for d in dups)
