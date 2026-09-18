"""grep\'s 0-results output names no case-insensitivity hint (#1777).

`grep` is case-sensitive throughout supertool. The instruction agents work
under says to confirm a write a second way, and grepping back a sentence
that IS on disk with different capitalisation returns
`0 results in 0 files, scanned N files` -- a false negative on a successful
write, produced by the exact step that exists to catch a failed one.

`0 results` is the one output where a case-insensitivity hint is worth
printing, because it is the only one where the caller cannot tell a real
absence from a spelling mismatch. Modelled on #1435\'s quoted-pattern note:
probed rather than asserted, so a genuine absence gets no extra noise.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import supertool
from _changelog_findable import assert_change_is_findable

MARKER = "case-sensitive"


def test_a_changelog_fragment_exists() -> None:
    assert_change_is_findable(1777)


@pytest.fixture()
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "c.txt").write_text(
        "alpha\nHello World\ngamma\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_grep_zero_on_a_differently_cased_pattern_names_case_sensitivity(
        tree: Path) -> None:
    out = supertool.dispatch("grep:hello world:c.txt:5")
    assert "0 results" in out, repr(out)
    assert MARKER in out, (
        "the zero carries `scanned 1 files` and nothing about case, so it "
        "reads as an absence in the file: " + repr(out))


def test_the_note_says_it_does_match_case_insensitively(tree: Path) -> None:
    out = supertool.dispatch("grep:hello world:c.txt:5")
    assert "DOES match" in out, (
        "a reader needs the deciding fact, not the hypothesis: " + repr(out))


def test_a_real_absence_gets_no_case_insensitivity_note(tree: Path) -> None:
    """The state that keeps the note from being noise."""
    out = supertool.dispatch("grep:nowhere at all:c.txt:5")
    assert "0 results" in out, repr(out)
    assert MARKER not in out, (
        "a genuine absence is not about case, and must not be told so: "
        + repr(out))


def test_a_case_matching_hit_gets_no_note(tree: Path) -> None:
    """The search succeeded as typed -- there is nothing to disclose."""
    out = supertool.dispatch("grep:Hello World:c.txt:5")
    assert "1 results" in out, repr(out)
    assert MARKER not in out, repr(out)


def test_grep_count_only_carries_the_same_note(tree: Path) -> None:
    out = supertool.op_grep("hello world", "c.txt", limit=5, count_only=True)
    assert "0 total matches" in out, repr(out)
    assert MARKER in out, repr(out)


def test_grep_with_context_carries_the_same_note(tree: Path) -> None:
    out = supertool.dispatch("grep_around:hello world:c.txt:1:5")
    assert "0 results" in out, repr(out)
    assert MARKER in out, repr(out)


def test_the_note_would_not_appear_if_the_op_did_nothing(tree: Path) -> None:
    """Guard against a test that passes on an unpatched tree."""
    out = supertool.dispatch("grep:alpha:c.txt:5")
    assert "1 results" in out, repr(out)
    assert MARKER not in out, repr(out)
