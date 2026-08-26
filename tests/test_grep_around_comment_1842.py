"""#1842 -- the comment above `_PATH_ARG_POSITIONS["grep_around"]` claims a
reason ("trailing slots must parse as ints, so a ':' in the pattern fails the
call rather than moving the path") that is true of the three-and-four-token
shapes and false of the two-token one: `grep_around:PATTERN:PATH` has no
trailing int slot to fail on, so a ':' in the pattern moves the path exactly
as `grep` does. Nothing fails, and the path did move.
"""

from __future__ import annotations

from pathlib import Path

import supertool
import _supertool


def test_two_token_shape_moves_the_path_without_failing(tmp_path: Path) -> None:
    """The behaviour the old comment said could not happen: a two-token
    `grep_around:PATTERN:PATH` call reaches the path and answers, it does not
    fail an int() parse."""
    f = tmp_path / "code.py"
    f.write_text("needle\nhay\n", encoding="utf-8")
    out = supertool.dispatch(f"grep_around:needle:{f}")
    assert "invalid literal for int" not in out, out
    assert "not found" not in out, out
    assert "needle" in out, out


def test_the_comment_names_both_shapes() -> None:
    """The corrected comment must not claim, unqualified, that the trailing
    slots parsing as ints is why the two-token shape is safe -- it must say
    the two-token shape moves the path exactly like `grep` does."""
    src = Path(_supertool.__file__).read_text(encoding="utf-8")
    idx = src.index('"grep_around": (2,),')
    comment = src[max(0, idx - 700):idx]
    assert "two-token" in comment or "two token" in comment, comment
    assert "grep" in comment, comment
