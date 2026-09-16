"""#2535 (gate-3 release-audit round 2, class B: a guard that did not run).

`_has_outer_wrapped_unbounded_group` (#1311) resolves a `(...)` group by
scanning to its matching close-paren and then jumps straight past it
(`i = j`), so a nested group buried inside an outer group that is itself
NOT quantified -- e.g. `(x(a+)+y)`, where the outer `( ... )` has no
trailing `+`/`*` of its own but the inner `(a+)` does -- was never
independently examined for its own trailing quantifier. All 5 existing
cases in test_grep_backtracking_guard_outer_wrap_1311.py have the OUTER
group itself quantified, so none of them exercise this shape.

`(x(a+)+y)` is confirmed catastrophic: elapsed against 26 `a`s doubled per
character (measured against HEAD in the filing issue, #2535).

Fixed by advancing the outer scan one character at a time instead of
jumping past a resolved group, so every `(` in the pattern -- at any
nesting depth -- gets its own matching-close-paren-plus-trailing-quantifier
check, not only the outermost one encountered at each position.
"""
from __future__ import annotations

import _supertool


def test_nested_unbounded_group_inside_unquantified_outer_group_is_refused():
    """The exact shape this issue reports."""
    result = _supertool._op_grep("(x(a+)+y)", path=".", limit=1)
    assert "catastrophic backtracking" in result, (
        "a nested (a+)+ inside an unquantified outer group was NOT refused: "
        + repr(result))


def test_nested_unbounded_group_inside_non_capturing_outer_group_is_refused():
    result = _supertool._op_grep("(?:(a+)+)", path=".", limit=1)
    assert "catastrophic backtracking" in result


def test_nested_unbounded_star_group_inside_optional_outer_group_is_refused():
    """A prefix literal and a mid-pattern nested group must not hide it."""
    result = _supertool._op_grep("foo(bar(a+)*)?", path=".", limit=1)
    assert "catastrophic backtracking" in result


def test_bare_optional_group_is_still_not_refused():
    """Positive control: must not regress #1311's own fix."""
    result = _supertool._op_grep("(a+)?", path=".", limit=1)
    assert "catastrophic backtracking" not in result


def test_two_independent_optional_groups_are_still_not_refused():
    """Positive control: must not regress #1311's own fix."""
    result = _supertool._op_grep("(a+)?b(c+)?", path=".", limit=1)
    assert "catastrophic backtracking" not in result
