"""Issue #229 — blank-line numbers must use the same right-aligned padding
as non-blank lines across all line-numbered read ops."""
from __future__ import annotations

from pathlib import Path

import supertool


WIDTH = 6  # current padding width used by `{i + 1:>6}` across ops


def _make_file(tmp_path: Path) -> Path:
    """File with mixed blank/non-blank lines around line 425 (matches issue example)."""
    lines = []
    for i in range(1, 440):
        if i in (426, 429, 441):
            lines.append("")
        else:
            lines.append(f"  code line {i}")
    p = tmp_path / "blanks.txt"
    p.write_text("\n".join(lines) + "\n")
    return p


def _line_number_columns(rendered: str) -> list[tuple[int, int]]:
    """For each rendered line starting with a (possibly padded) integer,
    return (lineno, leading-space-count). Skips headers/footers."""
    out: list[tuple[int, int]] = []
    for raw in rendered.splitlines():
        # match optional spaces, digits, then marker (space or arrow)
        stripped = raw.lstrip(" ")
        if not stripped or not stripped[0].isdigit():
            continue
        # split digits from rest
        digits = ""
        for ch in stripped:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            continue
        leading = len(raw) - len(stripped)
        out.append((int(digits), leading))
    return out


def test_around_line_blanks_aligned(tmp_path: Path) -> None:
    f = _make_file(tmp_path)
    out = supertool.op_around_line(str(f), 425, 10)
    cols = _line_number_columns(out)
    assert cols, "no numbered lines parsed"
    widths = {leading + len(str(n)) for n, leading in cols}
    assert widths == {WIDTH}, f"line-number column width inconsistent: {widths}\n{out}"


def test_read_offset_limit_blanks_aligned(tmp_path: Path) -> None:
    f = _make_file(tmp_path)
    out = supertool.render_file(str(f), offset=420, limit=15)
    cols = _line_number_columns(out)
    assert cols
    widths = {leading + len(str(n)) for n, leading in cols}
    assert widths == {WIDTH}, f"render_file column width inconsistent: {widths}\n{out}"


def test_tail_blanks_aligned(tmp_path: Path) -> None:
    f = _make_file(tmp_path)
    out = supertool.op_tail(str(f), 20)
    cols = _line_number_columns(out)
    assert cols
    widths = {leading + len(str(n)) for n, leading in cols}
    assert widths == {WIDTH}, f"op_tail column width inconsistent: {widths}\n{out}"


def test_head_blanks_aligned(tmp_path: Path) -> None:
    # head over file where blanks fall inside the window
    p = tmp_path / "head_blanks.txt"
    p.write_text("a\n\nb\n\nc\nd\n")
    out = supertool.op_head(str(p), 6)
    cols = _line_number_columns(out)
    assert cols
    widths = {leading + len(str(n)) for n, leading in cols}
    assert widths == {WIDTH}, f"op_head column width inconsistent: {widths}\n{out}"


def test_between_pattern_blanks_aligned(tmp_path: Path) -> None:
    p = tmp_path / "between_blanks.txt"
    p.write_text("START\nfoo\n\nbar\n\nEND\n")
    out = supertool.op_between_pattern("START", "END", str(p))
    cols = _line_number_columns(out)
    assert cols
    widths = {leading + len(str(n)) for n, leading in cols}
    assert widths == {WIDTH}, f"op_between_pattern column width inconsistent: {widths}\n{out}"
