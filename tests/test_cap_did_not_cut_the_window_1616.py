"""The byte cap only ever stops the NEXT line, so it cannot have cut a window
that ended anyway (#1616).

`grep` names `read:PATH:N-N` as the way back to a line it truncated, and
promises byte-exactness (#1489). The read delivers every byte -- and then said
`cut short by the 20000-byte cap`, because `capped` is set the moment the
running total crosses the cap, whether or not any line was left to drop. A
caller following the remedy read "still truncated" and re-anchored elsewhere.

The cap is checked AFTER a whole line has been appended, so no line is ever
truncated by it. It drops lines. When the last line read was the last one the
window asked for, or the last one in the file, it dropped none.
"""

from __future__ import annotations

import re
from pathlib import Path

import supertool

_WINDOW_RE = re.compile(r"^window: .*$", re.M)


def _window(out: str) -> str:
    m = _WINDOW_RE.search(out)
    assert m, f"no window note in:\n{out[:2000]}"
    return m.group(0)


def _fat(tmp_path: Path) -> Path:
    """Line 2 is 25 KB -- over the 20000-byte cap on its own, and the file has
    lines after it, so nothing but the requested window ends the read."""
    f = tmp_path / "mid.txt"
    f.write_text("a\n" + "N" * 25000 + "NEEDLE\n" + "c\n" + "d\n")
    return f


def test_a_fully_delivered_window_is_not_reported_as_cut(tmp_path: Path) -> None:
    f = _fat(tmp_path)
    out = supertool.dispatch(f"read:{f}:2-2")
    note = _window(out)
    assert "NEEDLE" in out, "the fixture did not return the whole line"
    assert "cut short" not in note, note
    assert "cannot be told apart" not in note, note
    assert "the limit was reached" in note, note


def test_a_fully_delivered_window_has_no_truncated_footer(tmp_path: Path) -> None:
    """The footer made the same claim and added a count of lines nobody asked
    for: `truncated at 20000 bytes -- showed lines 2-2 of 4 (2 more lines)`."""
    f = _fat(tmp_path)
    out = supertool.dispatch(f"read:{f}:2-2")
    assert "truncated at" not in out, out[:1500]


def test_the_cap_is_still_disclosed_when_it_cut_nothing(tmp_path: Path) -> None:
    """Silence would be the other half of the same defect: the caller's output
    IS at the cap, and a read one line wider will lose lines. Say what
    happened, do not claim it cost something."""
    f = _fat(tmp_path)
    note = _window(supertool.dispatch(f"read:{f}:2-2"))
    assert "20000-byte cap" in note, note
    assert "dropped nothing" in note, note


def test_greps_byte_exact_remedy_is_not_contradicted(tmp_path: Path) -> None:
    """End to end, the round trip #1616 is about: grep cuts the line, names
    the read, the read returns every byte and says so."""
    f = _fat(tmp_path)
    (tmp_path / "other.md").write_text("NEEDLE\n")
    grep_out = supertool.dispatch(f"grep:NEEDLE:{tmp_path}")
    assert "byte-exact" in grep_out
    m = re.search(r"read:(\S+):(\d+)-(\d+)", grep_out)
    assert m, grep_out
    read_out = supertool.dispatch(f"read:{m.group(1)}:{m.group(2)}-{m.group(3)}")
    assert "NEEDLE" in read_out
    assert "cut short" not in _window(read_out), _window(read_out)


def test_a_filtered_read_does_not_blame_the_cap_for_the_limits_work(
        tmp_path: Path) -> None:
    """The filtered branch made the same misattribution one render over.

    `grep=` scanning stops when the cap breaks the loop, so the unsearched
    lines are genuinely the cap's doing -- but only then. Here the LIMIT ended
    the scan and the fourth line happened to carry the total over the cap, so
    the cap dropped nothing and the line left unsearched is the limit's. Found
    in review of the fix above, not by the issue.
    """
    f = tmp_path / "wide.txt"
    f.write_bytes(b"".join(b"needle " + b"x" * 6000 + b"\n" for _ in range(5)))
    out = supertool.dispatch(f"read:{f}:0:4:grep=needle")
    assert "reached the 20000-byte cap, so the other" not in out, out[:1200]
    assert "outside that range" in out, out[:1200]


def test_a_cap_that_did_drop_lines_still_says_cut_short(tmp_path: Path) -> None:
    """The control. 200 fat lines, 150 asked for, ~49 fit: the cap really did
    end this window before the limit or the file did, and must keep saying
    so -- a fix that stopped reporting a real truncation would be the same
    defect pointing the other way."""
    f = tmp_path / "big.txt"
    f.write_bytes(b"".join(b"L%04d " % i + b"x" * 400 + b"\n"
                           for i in range(1, 201)))
    out = supertool.dispatch(f"read:{f}:5:150")
    note = _window(out)
    assert "cut short by the 20000-byte cap" in note, note
    assert "dropped nothing" not in note, note
    assert "truncated at" in out
