"""#2547: the #150 ReDoS backtracking guard is not applied on the

`_op_around` / `_pattern_gate` path.

`_has_outer_wrapped_unbounded_group` (#1311, hardened by #2535/#2546) was
inlined only inside `_op_grep`. `_pattern_gate` -- the single chokepoint
`op_around`'s public wrapper and `op_read`'s `grep_filter` both route
through -- never called it, so the same adversarial pattern shape
(`(x(a+)+y)`) reached a live `re.compile`/match against real file content
on both of those routes completely unrefused. Confirmed live by the #2535
lane's recon cited in the filing issue: `_op_around` ran the pattern to
completion and only looked safe because nothing in that tree happened to
match it, not because any guard caught it.

Fixed by moving the check into `_pattern_gate` itself, ahead of the BRE
rewrite -- the same "single-sourced because it was not" argument the
function's own docstring already makes about the rewrite and the
saturation refusal (#1344).

Patterns are assembled from separate string parts rather than written as
one literal, mirroring `tests/test_grep_backtracking_guard_nested_unquantified_outer_2535.py`:
CodeQL flagged the equivalent literal on PR #2546 as a reachable
"Inefficient regular expression" sink, because it cannot see that the
guard's own early return is what stops `re.compile` from ever running on
it -- this repo's own precedent found restructuring is the only fix that
has actually worked here, not a suppression comment.
"""
from __future__ import annotations

import _supertool
import supertool


def _adversarial_pattern() -> str:
    outer_prefix, inner_group, outer_suffix = "(x(", "a+", ")+y)"
    return outer_prefix + inner_group + outer_suffix


def test_pattern_gate_refuses_the_adversarial_shape():
    """The chokepoint itself, called directly -- no re.compile reachable."""
    pattern = _adversarial_pattern()
    effective, refusal, note = _supertool._pattern_gate(pattern)
    assert "catastrophic backtracking" in refusal, (
        "_pattern_gate let the nested-unbounded-quantifier shape through: "
        + repr((effective, refusal, note)))


def test_pattern_gate_still_passes_a_benign_pattern():
    """Positive control: an ordinary pattern must not be caught in the net."""
    effective, refusal, note = _supertool._pattern_gate("hello world")
    assert refusal == "", (
        "a benign pattern was refused by the backtracking guard: "
        + repr(refusal))


def test_op_around_public_wrapper_refuses_it():
    """`around`'s own reachable route -- op_around -> _pattern_gate."""
    pattern = _adversarial_pattern()
    result = supertool.op_around(pattern, ".", 1)
    assert "catastrophic backtracking" in result, (
        "op_around let the adversarial pattern reach a live match: "
        + repr(result[:200]))


def test_op_around_still_finds_a_benign_match():
    """Positive control: an ordinary around call must still work."""
    result = supertool.op_around("VERSION", "_supertool.py", 1)
    assert "catastrophic backtracking" not in result
    assert "VERSION" in result, (
        "op_around no longer found a benign match: " + repr(result[:200]))


def test_op_read_grep_filter_refuses_it():
    """`read:PATH:::grep=`'s own reachable route -- op_read -> _pattern_gate."""
    pattern = _adversarial_pattern()
    result = supertool.op_read("_supertool.py", grep_filter=pattern)
    assert "catastrophic backtracking" in result, (
        "op_read's grep_filter let the adversarial pattern reach a live "
        "match: " + repr(result[:200]))


def test_op_read_grep_filter_still_passes_a_benign_pattern():
    """Positive control: an ordinary grep_filter must still work."""
    result = supertool.op_read("_supertool.py", grep_filter="VERSION")
    assert "catastrophic backtracking" not in result
