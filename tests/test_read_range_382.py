"""read:PATH:START-END range form, and the misread-range warning (#382).

`read:PATH:A:B` is OFFSET:LIMIT but reads like START:END to anyone who has used
`sed -n 'A,Bp'`. The range form makes the intent explicit at the call site; the
note catches the misread when the old form is used.
"""

from __future__ import annotations

from pathlib import Path

import supertool


def _numbered(tmp_path: Path, name: str, count: int) -> Path:
    f = tmp_path / name
    f.write_bytes(("\n".join(f"line{i}" for i in range(1, count + 1)) + "\n").encode())
    return f


# ---------------------------------------------------------------------------
# range form
# ---------------------------------------------------------------------------

def test_range_form_returns_inclusive_span(tmp_path: Path) -> None:
    f = _numbered(tmp_path, "many.txt", 10)
    out = supertool.dispatch(f"read:{f}:3-5")
    assert "     3→line3" in out
    assert "     5→line5" in out
    assert "line2" not in out
    assert "line6" not in out


def test_range_form_single_line(tmp_path: Path) -> None:
    f = _numbered(tmp_path, "many.txt", 10)
    out = supertool.dispatch(f"read:{f}:4-4")
    assert "     4→line4" in out
    assert "line3" not in out
    assert "line5" not in out


def test_range_form_clamps_at_eof(tmp_path: Path) -> None:
    f = _numbered(tmp_path, "many.txt", 5)
    out = supertool.dispatch(f"read:{f}:4-99")
    assert "     4→line4" in out
    assert "     5→line5" in out
    assert "ERROR" not in out


def test_range_form_start_below_one_errors(tmp_path: Path) -> None:
    f = _numbered(tmp_path, "many.txt", 10)
    out = supertool.dispatch(f"read:{f}:0-5")
    assert "ERROR: read range START must be >= 1" in out


def test_range_form_end_before_start_errors(tmp_path: Path) -> None:
    f = _numbered(tmp_path, "many.txt", 10)
    out = supertool.dispatch(f"read:{f}:5-3")
    assert "ERROR: read range END (3) is before START (5)" in out


def test_range_form_rejects_trailing_limit(tmp_path: Path) -> None:
    f = _numbered(tmp_path, "many.txt", 10)
    out = supertool.dispatch(f"read:{f}:3-5:2")
    assert "takes no LIMIT" in out


def test_range_form_accepts_trailing_full(tmp_path: Path) -> None:
    f = _numbered(tmp_path, "many.txt", 10)
    out = supertool.dispatch(f"read:{f}:3-5:full")
    assert "ERROR" not in out
    assert "     3→line3" in out


def test_range_form_composes_with_grep_filter(tmp_path: Path) -> None:
    """The range consumes one slot, so the filter lands in parts[3] — it must be
    found there and not mistaken for a LIMIT."""
    f = _numbered(tmp_path, "many.txt", 30)
    out = supertool.dispatch(f"read:{f}:3-25:grep=line13")
    assert "ERROR" not in out
    assert "    13→line13" in out
    assert "    12→line12" not in out  # filtered out
    assert "    14→line14" not in out


def test_range_form_still_bounds_a_grep_filter(tmp_path: Path) -> None:
    """The filter narrows within the range; it does not widen past it."""
    f = _numbered(tmp_path, "many.txt", 30)
    out = supertool.dispatch(f"read:{f}:3-25:grep=line2")
    assert "    20→line20" in out
    assert "     2→line2" not in out  # matches the filter, but precedes the range
    assert "    26→line26" not in out  # matches the filter, but follows the range


def test_range_form_composes_with_triple_colon_grep(tmp_path: Path) -> None:
    """`:::grep=` puts the filter in parts[5] once a range is present."""
    f = _numbered(tmp_path, "many.txt", 30)
    out = supertool.dispatch(f"read:{f}:3-25:::grep=line1")
    assert "ERROR" not in out
    assert "    13→line13" in out


def test_offset_limit_form_still_finds_grep_filter(tmp_path: Path) -> None:
    """Backwards compatibility for the documented spelling."""
    f = _numbered(tmp_path, "many.txt", 30)
    out = supertool.dispatch(f"read:{f}:::grep=line1")
    assert "ERROR" not in out
    assert "    13→line13" in out


def test_offset_limit_form_still_offset_limit(tmp_path: Path) -> None:
    """Backwards compatibility: `:A:B` keeps its documented meaning."""
    f = _numbered(tmp_path, "many.txt", 10)
    out = supertool.dispatch(f"read:{f}:3:2")
    assert "     4→line4" in out
    assert "     5→line5" in out
    assert "line3" not in out
    assert "line6" not in out


# ---------------------------------------------------------------------------
# misread-range note
# ---------------------------------------------------------------------------

def test_note_fires_on_misread_range(tmp_path: Path) -> None:
    """The exact shape from the issue: intent 52-72, actual offset 52 limit 72."""
    f = _numbered(tmp_path, "many.txt", 100)
    out = supertool.dispatch(f"read:{f}:52:72")
    assert "OFFSET:LIMIT, not START:END" in out
    assert f"read:{f}:52-72" in out
    assert "note: read 48 lines (offset 52, limit 72)" in out


def test_no_note_when_window_fits(tmp_path: Path) -> None:
    """A legitimate offset/limit that lands inside the file stays quiet."""
    f = _numbered(tmp_path, "many.txt", 100)
    out = supertool.dispatch(f"read:{f}:10:20")
    assert "OFFSET:LIMIT, not START:END" not in out


def test_no_note_when_limit_below_offset(tmp_path: Path) -> None:
    """Overshoots EOF, but limit < offset — the ordinary 'read the tail' call."""
    f = _numbered(tmp_path, "many.txt", 100)
    out = supertool.dispatch(f"read:{f}:90:20")
    assert "OFFSET:LIMIT, not START:END" not in out


def test_no_note_on_range_form(tmp_path: Path) -> None:
    f = _numbered(tmp_path, "many.txt", 100)
    out = supertool.dispatch(f"read:{f}:52-72")
    assert "OFFSET:LIMIT, not START:END" not in out


def test_no_note_on_missing_file(tmp_path: Path) -> None:
    out = supertool.dispatch(f"read:{tmp_path / 'nope.txt'}:52:72")
    assert "ERROR: file not found" in out
    assert "OFFSET:LIMIT, not START:END" not in out


def test_no_note_without_offset(tmp_path: Path) -> None:
    f = _numbered(tmp_path, "many.txt", 10)
    out = supertool.dispatch(f"read:{f}:0:500")
    assert "OFFSET:LIMIT, not START:END" not in out
