"""#1311 self-review finding -- narrowing the backtracking guard to drop `?`
from the trailing-quantifier class reopened a real ReDoS hole for an OUTER
`+`/`*` wrapping a group whose own content contains an unbounded quantifier
at a DEEPER nesting level, e.g. `((a+)?)+`.

The single-level regex the guard used (a `(...)` group, then an inner `+`/`*`,
then a close-paren, then a trailing quantifier) cannot see past the FIRST
close-paren it encounters, so scanning from the outer `(` in `((a+)?)+` it
can only ever match up to the INNER close-paren, whose own trailing
character is `?` -- never `+`/`*` -- so the regex never inspects the outer
group's real trailing quantifier at all. The old, over-eager regex
accidentally caught this shape only as a side effect of blanket-refusing
every optional-wrapped-unbounded fragment anywhere in the pattern, not
because it understood the outer wrapping -- so narrowing away the `?` case
removed the accidental catch along with it and added nothing back.

`((a+)?)+` is confirmed catastrophic: matching it against 25 `a`s followed by
a non-matching character took several seconds on this machine (measured;
#1311 self-review).

Fixed by replacing the single-level regex with a bracket-depth-aware scan
that flags a `(...)` group followed by an outer `+`/`*` when the group's own
content contains an UNBOUNDED quantifier (`+`/`*`) anywhere inside it, at any
nesting depth -- not only directly inside the immediately-enclosing parens.
"""
from __future__ import annotations

import _supertool


def test_outer_plus_wrapping_an_optional_unbounded_group_is_refused():
    """The exact shape this self-review finding reports."""
    result = _supertool._op_grep("((a+)?)+", path=".", limit=1)
    assert "catastrophic backtracking" in result, (
        "an outer `+` wrapping `(a+)?` was NOT refused: " + repr(result))


def test_outer_star_wrapping_an_optional_unbounded_group_is_refused():
    result = _supertool._op_grep("((a*)?)*", path=".", limit=1)
    assert "catastrophic backtracking" in result


def test_outer_plus_wrapping_prefix_plus_optional_unbounded_group_is_refused():
    """A sibling literal inside the outer group must not hide the risk."""
    result = _supertool._op_grep("(x(a+)?)+", path=".", limit=1)
    assert "catastrophic backtracking" in result


def test_bare_optional_group_is_still_not_refused():
    """Positive control: item 1's own fix must not regress."""
    result = _supertool._op_grep("(a+)?", path=".", limit=1)
    assert "catastrophic backtracking" not in result


def test_two_independent_optional_groups_are_still_not_refused():
    """Positive control: item 1's own fix must not regress."""
    result = _supertool._op_grep("(a+)?b(c+)?", path=".", limit=1)
    assert "catastrophic backtracking" not in result
