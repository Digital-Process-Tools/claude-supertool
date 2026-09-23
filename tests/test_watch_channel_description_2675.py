"""#2675: the `channel:health` op description in presets/watch.json still says
"five states" and omits the sixth, `BOUND, UNPROVEN` (exit 8, added by #2658).

`help:channel` and `ops:full` render this string, which is the only doc surface
those two commands offer for the op -- a caller who branches on "the first line
the way the description says" has no key for the sixth state until this string
names it. presets/watch/channel.py's own module docstring and RC_UNPROVEN
comment were already updated in #2663; this is the one site that lagged.
"""
from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _channel_description() -> str:
    watch_json = json.loads(
        (REPO_ROOT / "presets" / "watch.json").read_text(encoding="utf-8")
    )
    ops = watch_json["ops"]
    if "channel" not in ops:
        raise AssertionError("no 'channel' op found in presets/watch.json")
    return ops["channel"]["description"]


def test_description_names_six_states_not_five():
    description = _channel_description()
    assert "six states" in description, (
        "description still says the old count instead of six -- "
        f"description was: {description[:200]!r}..."
    )
    assert "five states" not in description


def test_description_names_bound_unproven_and_exit_8():
    description = _channel_description()
    assert "BOUND, UNPROVEN" in description
    assert "0/1/3/4/5/8" in description, (
        "the exit-code list does not name 8 alongside the others: "
        f"{description!r}"
    )


def test_description_first_line_list_includes_bound_unproven():
    description = _channel_description()
    # The "Branch on the report's first line" sentence enumerates the exact
    # verdict words a caller should key on -- BOUND, UNPROVEN must be one of
    # them, not just mentioned in passing elsewhere in the description.
    assert "Branch on the report's first line" in description
    first_line_sentence = description.split("Branch on the report's first line", 1)[1]
    first_line_sentence = first_line_sentence.split(".", 1)[0]
    assert "BOUND, UNPROVEN" in first_line_sentence, (
        "the enumerated first-line list does not name BOUND, UNPROVEN: "
        f"{first_line_sentence!r}"
    )
