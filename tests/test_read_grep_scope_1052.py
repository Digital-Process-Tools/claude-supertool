"""#1052 — `read:PATH:::grep=X` answered `(no lines matching 'X')` about a file
whose line 328 contains X.

The filter is not broken. The *window* is. `render_file` defaults LIMIT to
`read.max_lines` (300) when the caller gives none, and the filter runs inside
that window — so on a 351-line file the inline filter looked at lines 1-300,
never saw line 328, and reported its zero in the tool's own voice with no
mention of the 51 lines it had not read.

That is this repository's most-filed defect class in a read primitive: an
absence produced by the tool, rendered as an absence in the world. The sibling
`grep` op's zero carries `scanned N files`; this one carried nothing.

Two pins, and neither would pass against a render that merely produced *some*
output:

* the filter must search the whole file when the caller named no window, and
* whenever it did *not* search the whole file, the zero must say so.

A third pin covers the same class one layer up: an invalid regex is silently
downgraded to a literal search, and the literal's zero is then indistinguishable
from a real absence.
"""

from __future__ import annotations

from pathlib import Path

import supertool
import _supertool


def _file_with_match_past_the_cap(tmp_path: Path) -> Path:
    """A file longer than `read.max_lines`, with the only match at the end."""
    cap = _supertool._get_op_int("read", "max_lines", _supertool.MAX_READ_LINES)
    lines = [b"filler %d\n" % i for i in range(1, cap + 40)]
    lines.append(b"registered_and_documented\n")
    f = tmp_path / "long.txt"
    f.write_bytes(b"".join(lines))
    return f


def test_inline_grep_finds_a_match_past_the_default_line_cap(
    tmp_path: Path,
) -> None:
    """The reported call. The substring is on the last line of a file longer
    than the default window; the answer must be the line, not a zero."""
    f = _file_with_match_past_the_cap(tmp_path)
    out = supertool.dispatch(f"read:{f}:::grep=registered_and_documented")
    assert "no lines matching" not in out, out
    assert "registered_and_documented" in out
    # ...and the line number must be the real one, not a window-relative index.
    cap = _supertool._get_op_int("read", "max_lines", _supertool.MAX_READ_LINES)
    assert f"{cap + 40:>6}→registered_and_documented" in out


def test_a_partial_filter_scan_says_it_was_partial(tmp_path: Path) -> None:
    """When the caller *does* bound the window, a zero inside it is still a
    zero about the window only. It must never read as a fact about the file."""
    f = _file_with_match_past_the_cap(tmp_path)
    out = supertool.dispatch(f"read:{f}:0:10:grep=registered_and_documented")
    assert "no lines matching" in out
    assert "NOT searched" in out, out
    assert "lines 1-10" in out
    # The total has to be present so the reader can size what was missed.
    # Anchored on `of `: an unanchored number would also match the digits of a
    # Windows tmp_path and pass without the disclosure existing.
    total = _supertool._get_op_int(
        "read", "max_lines", _supertool.MAX_READ_LINES) + 40
    assert f"of {total}" in out


def test_a_full_filter_scan_says_it_was_full(tmp_path: Path) -> None:
    """The honest zero is still allowed to be a zero — and has to distinguish
    itself from the partial one, or the disclosure buys nothing."""
    f = tmp_path / "short.txt"
    f.write_bytes(b"alpha\nbeta\ngamma\n")
    out = supertool.dispatch(f"read:{f}:::grep=delta")
    assert "no lines matching" in out
    assert "NOT searched" not in out
    assert "in any of 3 lines" in out


def test_an_unusable_regex_is_not_reported_as_an_absence(tmp_path: Path) -> None:
    """`re.error` falls back to a literal search. That is a reasonable rescue
    and an unreasonable silence: the caller's pattern was rejected, and the
    literal's zero looks exactly like a real absence."""
    f = tmp_path / "s.txt"
    f.write_bytes(b"alpha\nbeta\n")
    out = supertool.dispatch(f"read:{f}:::grep=alpha(")
    assert "no lines matching" in out
    assert "literal" in out, out
