"""The colon split is disclosed by `around` too, and above the answer (#1821).

Filed against `grep`: a pattern containing `:` is split across the positional
slots, and the report was said to arrive *above* the note explaining why. That
half does not reproduce — `op_grep` has prepended `_pattern_read_as_note` since
#1065, and #1166 added the path — so the ordering is pinned here rather than
fixed, because nothing asserted it and the next refactor would not have noticed.

What does reproduce is the same defect one op over. `around` shares
`_colon_split_hint`, so it refuses a PATH slot that does not exist; when the
slot *does* exist it rejoins the pattern exactly as grep does and then prints a
well-formed, plausible answer with no disclosure at all. That is strictly worse
than what was filed: not a note in the wrong place, no note.
"""
from __future__ import annotations

from pathlib import Path

import supertool


def _tree(tmp_path: Path) -> Path:
    """Two files, so a wrong split still returns a plausible non-zero answer.

    The mis-split reading has to *succeed* for this to be the bug it is; a
    fixture where the wrong pattern matches nothing would pass on a broken op.
    """
    d = tmp_path / "notes"
    d.mkdir()
    (d / "a.md").write_text("mode: remind\ntitle: alpha\n")
    (d / "b.md").write_text("mode: block\ntitle: beta\n")
    return d


def test_around_discloses_the_rejoined_pattern(tmp_path: Path) -> None:
    d = _tree(tmp_path)
    out = supertool.op_around("mode:.*remind|title", str(d), 1)
    assert "match at line" in out, (
        "positive control: the fixture must actually match, or the "
        "disclosure assertion below is about a dead harness: " + repr(out))
    assert "pattern read as 'mode:.*remind|title'" in out, (
        "around rejoins the colon-split pattern exactly as grep does and "
        "then answers without saying so (#1821): " + repr(out))
    assert repr(str(d)) in out, (
        "the split has two outputs and a receipt silent about which token "
        "became the PATH reads as complete (#1166): " + repr(out))


def test_around_plain_pattern_gets_no_note(tmp_path: Path) -> None:
    """The paired must-not-print case, in the same fixture as the must-print.

    A disclosure that fires on every call is noise, and noise is how a real
    one stops being read.
    """
    d = _tree(tmp_path)
    out = supertool.op_around("title", str(d), 1)
    assert "match at line" in out, (
        "same positive control: silence only means something once the op "
        "has been shown to answer here: " + repr(out))
    assert "pattern read as" not in out, repr(out)


def test_around_note_sits_above_the_first_answer(tmp_path: Path) -> None:
    d = _tree(tmp_path)
    out = supertool.op_around("mode:.*remind|title", str(d), 1)
    assert out.index("pattern read as") < out.index("matched"), (
        "a caveat under the number is a caveat the reader has to still be "
        "reading to benefit from (#1821): " + repr(out))


def test_around_names_around_when_it_absorbs_a_re_prefix(tmp_path: Path) -> None:
    """`between:re:START:END:PATH` is the op with the prefix — not this one.

    The sub-note is worth nothing if it tells an `around` caller about grep.
    """
    d = _tree(tmp_path)
    out = supertool.op_around("re:alpha|beta", str(d), 1)
    assert "around has no `re:` prefix" in out, repr(out)
    assert "grep has no" not in out, repr(out)


def test_grep_note_still_sits_above_the_report_line(tmp_path: Path) -> None:
    """The half of #1821 that did not reproduce, pinned so it cannot regress."""
    d = _tree(tmp_path)
    out = supertool.op_grep("mode:.*remind|title", str(d), limit=10)
    assert "results in" in out, repr(out)
    assert out.index("pattern read as") < out.index("results in"), repr(out)
