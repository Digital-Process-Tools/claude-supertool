"""`between` given a line range, and the range form's own trailer (#983).

`between` takes SYMBOL:PATH. Handed `between:PATH:START:END` it reads END as a
path and reports a filesystem error about a number — an absence produced by the
tokenizer, dressed as an absence on disk. The range form it should have named,
`read:PATH:START-END`, already exists; nothing on the failing path says so.

The same range form, used correctly, was being told it was wrong: the window
note's OFFSET lecture fired on a call that never typed an OFFSET, and named a
span one line off from the one that was asked for.
"""

from __future__ import annotations

from pathlib import Path

import supertool


def _numbered(tmp_path: Path, name: str, count: int) -> Path:
    f = tmp_path / name
    f.write_bytes(("\n".join(f"line{i}" for i in range(1, count + 1)) + "\n").encode())
    return f


# ---------------------------------------------------------------------------
# between:PATH:START:END — name the range form, not a missing file
# ---------------------------------------------------------------------------

def test_between_line_range_names_the_read_range_form(tmp_path: Path) -> None:
    f = _numbered(tmp_path, "many.txt", 40)
    out = supertool.dispatch(f"between:{f}:12:20")
    assert f"read:{f}:12-20" in out, out
    assert "does not take line ranges" in out, out


def test_between_line_range_does_not_claim_a_missing_path(tmp_path: Path) -> None:
    f = _numbered(tmp_path, "many.txt", 40)
    out = supertool.dispatch(f"between:{f}:12:20")
    assert "path not found: '20'" not in out, out
    # ...nor the payload lecture, which cannot fix a line range either.
    assert "colons and all" not in out, out


def test_between_single_line_names_around_line(tmp_path: Path) -> None:
    f = _numbered(tmp_path, "many.txt", 40)
    out = supertool.dispatch(f"between:{f}:12")
    assert f"around_line:{f}:12" in out, out


def test_between_symbol_mode_is_not_diverted(tmp_path: Path) -> None:
    src = tmp_path / "m.py"
    src.write_text("def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n")
    out = supertool.dispatch(f"between:alpha:{src}")
    assert "does not take line ranges" not in out, out
    assert "not found" not in out, out


def test_between_pattern_mode_still_works(tmp_path: Path) -> None:
    src = tmp_path / "m.py"
    src.write_text("head\nSTART\nmiddle\nEND\ntail\n")
    out = supertool.dispatch(f"between:re:START:END:{src}")
    assert "middle" in out, out
    assert "tail" not in out, out


def test_between_numeric_symbol_with_real_path_is_untouched(tmp_path: Path) -> None:
    """A digit-named symbol in a real file is symbol mode, not a range."""
    src = tmp_path / "n.py"
    src.write_text("def alpha():\n    return 1\n")
    out = supertool.dispatch(f"between:404:{src}")
    assert "does not take line ranges" not in out, out


# ---------------------------------------------------------------------------
# read:PATH:START-END — a correct call is not lectured
# ---------------------------------------------------------------------------

def test_range_form_is_not_told_it_used_offset(tmp_path: Path) -> None:
    f = _numbered(tmp_path, "many.txt", 200)
    out = supertool.dispatch(f"read:{f}:120-124")
    assert "120→line120" in out, out
    assert "OFFSET is a skip count" not in out, out


def test_range_form_keeps_its_window_disclosure(tmp_path: Path) -> None:
    f = _numbered(tmp_path, "many.txt", 200)
    out = supertool.dispatch(f"read:{f}:120-124")
    assert "returning lines 120-124 of 200" in out, out


def test_offset_form_still_gets_the_lecture(tmp_path: Path) -> None:
    f = _numbered(tmp_path, "many.txt", 200)
    out = supertool.dispatch(f"read:{f}:120:5")
    assert "OFFSET is a skip count" in out, out
