"""#2550: `_pattern_gate` has no pattern-length cap, so `around`/`read:grep=`

reach `re.compile` with any-length patterns.

`grep:PATTERN:PATH` refuses a pattern over 1000 characters before it ever
reaches `re.compile` -- but that check is inlined only inside `_op_grep`
(around line 7077). `_pattern_gate` is the single chokepoint `op_around`'s
public wrapper and `op_read`'s `grep_filter` both route through, and it has
no equivalent length check: an arbitrary-length pattern from either route
reaches a live `re.compile`/match against real file content with no
size-based backstop at all.

Fixed by moving the length cap into `_pattern_gate` itself, ahead of the
ReDoS backtracking guard -- the same "single-sourced because it was not"
argument the function's own docstring already makes about the rewrite, the
saturation refusal (#1344) and the backtracking guard (#2547).

The over-length pattern is built by repeating a short literal rather than
written out as one long literal, so the test file itself stays short.
"""
from __future__ import annotations

import _supertool
import supertool


def _overlong_pattern() -> str:
    return "a" * 5000


def test_pattern_gate_refuses_an_overlong_pattern():
    """The chokepoint itself, called directly -- no re.compile reachable."""
    pattern = _overlong_pattern()
    effective, refusal, note = _supertool._pattern_gate(pattern)
    assert "pattern too long" in refusal, (
        "_pattern_gate let an overlong pattern through: "
        + repr((effective, refusal, note)))


def test_pattern_gate_still_passes_an_ordinary_length_pattern():
    """Positive control: an ordinary-length pattern must not be caught."""
    effective, refusal, note = _supertool._pattern_gate("hello world")
    assert refusal == "", (
        "an ordinary-length pattern was refused by the length cap: "
        + repr(refusal))


def test_op_around_public_wrapper_refuses_it():
    """`around`'s own reachable route -- op_around -> _pattern_gate."""
    pattern = _overlong_pattern()
    result = supertool.op_around(pattern, ".", 1)
    assert "pattern too long" in result, (
        "op_around let an overlong pattern reach a live match: "
        + repr(result[:200]))


def test_op_around_still_finds_a_benign_match():
    """Positive control: an ordinary around call must still work."""
    result = supertool.op_around("VERSION", "_supertool.py", 1)
    assert "pattern too long" not in result
    assert "VERSION" in result, (
        "op_around no longer found a benign match: " + repr(result[:200]))


def test_op_read_grep_filter_refuses_it():
    """`read:PATH:::grep=`'s own reachable route -- op_read -> _pattern_gate."""
    pattern = _overlong_pattern()
    result = supertool.op_read("_supertool.py", grep_filter=pattern)
    assert "pattern too long" in result, (
        "op_read's grep_filter let an overlong pattern reach a live "
        "match: " + repr(result[:200]))


def test_op_read_grep_filter_still_passes_an_ordinary_pattern():
    """Positive control: an ordinary grep_filter must still work."""
    result = supertool.op_read("_supertool.py", grep_filter="VERSION")
    assert "pattern too long" not in result


def test_op_grep_still_refuses_it_through_the_single_source():
    """`grep`'s own route, now single-sourced through `_pattern_gate` too."""
    pattern = _overlong_pattern()
    result = supertool.op_grep(pattern, ".")
    assert "pattern too long" in result, (
        "op_grep no longer refuses an overlong pattern: "
        + repr(result[:200]))
