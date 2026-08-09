"""1078 — a note about the hunks may not out-claim the byte cap under it.

`gh-pr:N:diff:PATH` renders two things in one output: a note describing the
file's hunks, and — below it — the `GH_PR_DIFF_MAX_BYTES` cap's own statement
that what follows is not the whole file's diff. The note used to be written
unconditionally, so a capped render carried both `all hunks follow` and
`this is NOT the whole file's diff`, and nothing told the reader which to
believe.

The multi-entry sentence (#1068) is the same shape: `oldest first, so ... the
LAST occurrence is the current one` points the reader at the bottom of the
body, which is exactly the part the cap removes.

What is pinned here is not the wording but the impossibility: in a capped
render the complete-case sentences must not appear at all, whatever they are
later reworded to.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

PRESET_PATH = Path(__file__).parent.parent / "presets" / "github" / "_pr_diff.py"
_spec = importlib.util.spec_from_file_location("gh_pr_diff_1078", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
diff = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(diff)

#: Sentences that are only true when nothing was withheld.
COMPLETE_CASE_PHRASES = (
    "all hunks follow",
    "the LAST occurrence is the current one",
)

HUNK_A = "@@ -1,2 +1,2 @@\n-" + "x" * 400 + "\n+" + "y" * 400
HUNK_B = "@@ -80,2 +80,2 @@\n-" + "x" * 400 + "\n+" + "y" * 400


def _entry(**over: object) -> dict:
    entry = {
        "path": "a.py",
        "old_path": None,
        "status": "M",
        "added": 2,
        "removed": 2,
        "hunks": [HUNK_A, HUNK_B],
        "binary": False,
    }
    entry.update(over)
    return entry


def _render(max_bytes: int, entries: int = 1, **over: object) -> str:
    """A render of one path, optionally repeated `entries` times.

    The repetition goes in as separate records rather than an `entries` key:
    `render()` calls `coalesce()`, which recomputes that count, so an entry
    dict carrying it is silently overwritten and the multi-entry sentence
    never renders.
    """
    files = [_entry(**over) for _ in range(entries)]
    text, code = diff.render(files, header=["H"], path="a.py",
                             max_bytes=max_bytes)
    assert code == 0
    return " ".join(text.split())


def test_the_note_is_still_written_when_nothing_was_withheld() -> None:
    """The fix is not deletion: an uncapped render still says it is complete."""
    text = _render(1_000_000)
    assert "same edit x2" in text
    assert "all hunks follow" in text
    assert "NOT the whole file" not in text


def test_a_capped_render_never_claims_all_hunks_follow() -> None:
    text = _render(120)
    assert "NOT the whole file" in text, "the cap must still disclose itself"
    assert "all hunks follow" not in text


def test_a_capped_render_still_says_the_hunks_were_mechanical() -> None:
    """Losing the claim must not lose the note — the reader still needs it."""
    text = _render(120)
    assert "same edit x2" in text


def test_a_capped_render_says_the_note_describes_more_than_it_shows() -> None:
    """A note about hunks the reader cannot see has to say so."""
    text = _render(120).lower()
    assert "cap" in text
    assert "not all" in text or "not every" in text


def test_a_capped_multi_entry_render_does_not_point_at_a_withheld_bottom() -> None:
    """#1068's sentence is a completeness claim too, under the same cap."""
    text = _render(120, entries=3)
    assert "Assembled from 3 entries" in text
    assert "LAST" not in text


def test_an_uncapped_multi_entry_render_keeps_the_1068_wording() -> None:
    text = _render(1_000_000, entries=3)
    assert "the LAST occurrence is the current one" in text


@pytest.mark.parametrize("phrase", COMPLETE_CASE_PHRASES)
def test_no_complete_case_phrase_survives_a_cap(phrase: str) -> None:
    assert phrase not in _render(120, entries=2)


def test_a_binary_file_is_not_reported_as_capped() -> None:
    """No hunks means nothing to withhold — the capped wording must not leak."""
    text = _render(1, binary=True, hunks=[])
    assert "NOT the whole file" not in text
    assert "Binary file" in text
