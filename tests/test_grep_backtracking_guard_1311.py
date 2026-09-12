"""#1311 item 1 -- the catastrophic-backtracking guard refuses a bounded `?`.

`_op_grep` refuses a pattern matching a nested-unbounded-quantifier shape as
"nested unbounded quantifiers -- would risk catastrophic backtracking". That
refuses a group with an unbounded `+`/`*` inside wrapped in `?`, along with
the genuinely dangerous `(...+)+` / `(...+)*` shapes -- but `?` bounds the
group to at most ONE repetition, so there is no outer quantifier that can
re-partition the same input unboundedly and it cannot backtrack
catastrophically. Reported live against a real grep call whose pattern was
digits, a dot, an alternation of two words, an OPTIONAL group of a dot plus
one or more lowercase-or-hyphen characters, then a dot and "md".

The real risk is an OUTER `+` or `*` on a group whose own content is itself
unbounded (`+`/`*`) -- that is what lets the engine explore exponentially many
ways of re-splitting the same substring across repeated iterations of the
outer loop. `?` never iterates more than once, so it never gets that choice.

Decision recorded here (the issue calls this a judgment call): the exception
is scoped to the outer quantifier alone, per matched group, not to "does the
whole pattern only ever contain `?`-wrapped groups". `(a+)?b(c+)?` is two
independent, individually-bounded groups and must also pass -- rejected
alternative: only exempting a pattern if EVERY nested-unbounded group in it is
`?`-wrapped, which would still refuse a mixed pattern with no added safety,
since each group's own risk depends only on its own outer quantifier, not on
what quantifier some unrelated group elsewhere in the same pattern carries.
"""
from __future__ import annotations

import _supertool


def test_optional_group_with_unbounded_content_is_not_refused():
    """The exact shape from the issue: an optional group wrapping a `+`."""
    pattern = "[0-9]+.(added|fixed)(.[a-z-]+)?.md"
    result = _supertool._op_grep(pattern, path=".", limit=5)
    assert "catastrophic backtracking" not in result, (
        "a `?`-wrapped group was refused as catastrophic: " + repr(result))


def test_optional_group_alone_is_not_refused():
    pattern = "(a+)?"
    result = _supertool._op_grep(pattern, path=".", limit=1)
    assert "catastrophic backtracking" not in result


def test_two_independent_optional_groups_are_not_refused():
    """Two independently-bounded groups, not one combined risk."""
    pattern = "(a+)?b(c+)?"
    result = _supertool._op_grep(pattern, path=".", limit=1)
    assert "catastrophic backtracking" not in result


def test_genuinely_unbounded_nesting_is_still_refused():
    """Positive control: the real ReDoS shape must still refuse."""
    result = _supertool._op_grep("(a+)+", path=".", limit=1)
    assert "catastrophic backtracking" in result


def test_star_wrapped_unbounded_group_is_still_refused():
    result = _supertool._op_grep("(a+)*", path=".", limit=1)
    assert "catastrophic backtracking" in result
