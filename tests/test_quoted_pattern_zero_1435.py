"""A quoted pattern matched the quote characters and the zero said nothing (#1435).

    supertool "grep:'beta':/tmp/q.txt:5"   ->  (0 results in 0 files, scanned 1 files, limit 5)
    supertool "grep:beta:/tmp/q.txt:5"     ->  /tmp/q.txt:2:beta

`scanned 1 files` is the disclosure that says the op looked, so the zero reads as
a fact about the file. It was a fact about the pattern: the `'` characters were
searched for as literal text.

The choice made here is the third of the three the issue weighed, and it is the
only one that changes no result. Nothing is stripped and nothing is refused --
a caller hunting a genuinely quoted phrase is exactly right and gets their hits
untouched. What changes is the silence *after a zero*: the op says the pattern
is quote-wrapped, and probes the unwrapped spelling so the reader gets the
deciding fact rather than a hypothetical. Three states, not two:

  - the unwrapped pattern DOES match here -- the zero is about the quotes;
  - it matches nothing either -- the quotes are not why, the absence is real;
  - the pattern is not a wrapped pair at all -- no note.

The second state is the one that stops the note becoming noise: a reader who
quoted deliberately, and is genuinely looking at an absence, is told so rather
than being sent to re-run a search that will also come back empty.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import supertool
from _changelog_findable import assert_change_is_findable

MARKER = "searched as literal text"


def test_a_changelog_fragment_exists() -> None:
    assert_change_is_findable(1435)


@pytest.fixture()
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "q.txt").write_text(
        "alpha\nbeta\ngamma\nhas 'quoted' word\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_grep_zero_on_a_quoted_pattern_names_the_quotes(tree: Path) -> None:
    out = supertool.dispatch("grep:'beta':q.txt:5")
    assert "0 results" in out, repr(out)
    assert MARKER in out, (
        "the zero carries `scanned 1 files` and nothing about the quotes, so "
        "it reads as an absence in the file: " + repr(out))


def test_the_note_says_the_unquoted_pattern_does_match(tree: Path) -> None:
    out = supertool.dispatch("grep:'beta':q.txt:5")
    assert "DOES match" in out, (
        "a reader needs the deciding fact, not the hypothesis: " + repr(out))


def test_a_real_absence_is_not_blamed_on_the_quotes(tree: Path) -> None:
    """The state that keeps the note from being noise."""
    out = supertool.dispatch("grep:'nowhere':q.txt:5")
    assert MARKER in out, repr(out)
    assert "DOES match" not in out, repr(out)
    assert "nothing here either" in out, repr(out)


def test_a_genuinely_quoted_phrase_still_matches_and_gets_no_note(
        tree: Path) -> None:
    """Nothing is stripped: the caller who wants the quotes is served first."""
    out = supertool.dispatch("grep:'quoted':q.txt:5")
    assert "1 results" in out, repr(out)
    assert MARKER not in out, (
        "the search succeeded as typed -- there is nothing to disclose: "
        + repr(out))


def test_a_pattern_that_merely_starts_and_ends_with_a_quote_is_not_a_pair(
        tree: Path) -> None:
    """`'zzz'|'yyy'` is not a wrapped pattern, and stripping its ends is nonsense."""
    out = supertool.dispatch("grep:'zzz'|'yyy':q.txt:5")
    assert "0 results" in out, repr(out)
    assert MARKER not in out, repr(out)


def test_an_empty_quoted_pattern_is_not_treated_as_a_pair(tree: Path) -> None:
    out = supertool.op_grep("''", "q.txt", limit=5)
    assert MARKER not in out, repr(out)


def test_double_quotes_and_backticks_behave_like_single_quotes(
        tree: Path) -> None:
    for wrapped in ('"beta"', "`beta`"):
        out = supertool.op_grep(wrapped, "q.txt", limit=5)
        assert MARKER in out, (wrapped, repr(out))
        assert "DOES match" in out, (wrapped, repr(out))


def test_grep_with_context_carries_the_same_note(tree: Path) -> None:
    """grep_around is the same body with a different branch (#1435 sweep)."""
    out = supertool.dispatch("grep_around:'beta':q.txt:1:5")
    assert "0 results" in out, repr(out)
    assert MARKER in out, repr(out)


def test_grep_count_only_carries_the_same_note(tree: Path) -> None:
    out = supertool.op_grep("'beta'", "q.txt", limit=5, count_only=True)
    assert "0 total matches" in out, repr(out)
    assert MARKER in out, repr(out)


def test_around_carries_the_same_note(tree: Path) -> None:
    out = supertool.dispatch("around:'beta':q.txt:1")
    assert "no match" in out, repr(out)
    assert MARKER in out, repr(out)


def test_read_grep_filter_carries_the_same_note(tree: Path) -> None:
    out = supertool.dispatch("read:q.txt:::grep='beta'")
    assert "no lines matching" in out, repr(out)
    assert MARKER in out, repr(out)


def test_read_grep_filter_distinguishes_a_real_absence(tree: Path) -> None:
    out = supertool.dispatch("read:q.txt:::grep='nowhere'")
    assert MARKER in out, repr(out)
    assert "nothing here either" in out, repr(out)


def test_the_note_would_not_appear_if_the_op_did_nothing(tree: Path) -> None:
    """Guard against a test that passes on an unpatched tree: the unquoted
    spelling must be silent, so MARKER cannot be coming from boilerplate."""
    out = supertool.dispatch("grep:nowhere:q.txt:5")
    assert "0 results" in out, repr(out)
    assert MARKER not in out, repr(out)
