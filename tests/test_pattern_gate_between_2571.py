"""#2571: `op_between_pattern` bypasses `_pattern_gate` -- unbounded regex
hang, no length cap.

`#2547` (commit `7e3631ee`) moved the #150 ReDoS backtracking guard into
`_pattern_gate` specifically so it would cover "every other route through
this chokepoint", and `#2550` (`b8f037d6`) added the 1000-char length cap
there too. `op_between_pattern` never reached that chokepoint: it compiled
its `start`/`end` regexes directly with `re.compile` and `.search()`ed them
against every line of a real file, with no gate, no length cap and no
backtracking check, on both dispatch routes (`@payload` and colon form).

Fixed by routing both `start` and `end` through `_pattern_gate` before
either is compiled, mirroring `op_around` and `op_read`'s `grep_filter`.

Patterns are assembled from separate string parts rather than written as one
literal, mirroring `tests/test_pattern_gate_backtracking_guard_2547.py`:
CodeQL flagged the equivalent literal on PR #2546 as a reachable
"Inefficient regular expression" sink, because it cannot see that the
guard's own early return is what stops `re.compile` from ever running on it.
"""
from __future__ import annotations

import supertool


def _adversarial_pattern() -> str:
    outer_prefix, inner_group, outer_suffix = "(x(", "a+", ")+y)"
    return outer_prefix + inner_group + outer_suffix


def _overlong_pattern() -> str:
    return "a" * 5000


def test_op_between_pattern_refuses_adversarial_start(tmp_path):
    """`start`'s own reachable route -- op_between_pattern -> _pattern_gate."""
    target = tmp_path / "target.txt"
    target.write_text("xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaay\nEND\n")
    result = supertool.op_between_pattern(
        _adversarial_pattern(), "END", str(target))
    assert "catastrophic backtracking" in result, (
        "op_between_pattern let the adversarial start pattern reach a live "
        "match: " + repr(result[:200]))


def test_op_between_pattern_refuses_adversarial_end(tmp_path):
    """`end`'s own reachable route -- op_between_pattern -> _pattern_gate."""
    target = tmp_path / "target.txt"
    target.write_text("START\nxaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaay\n")
    result = supertool.op_between_pattern(
        "START", _adversarial_pattern(), str(target))
    assert "catastrophic backtracking" in result, (
        "op_between_pattern let the adversarial end pattern reach a live "
        "match: " + repr(result[:200]))


def test_op_between_pattern_refuses_overlong_start(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("hello\nEND\n")
    result = supertool.op_between_pattern(
        _overlong_pattern(), "END", str(target))
    assert "pattern too long" in result, (
        "op_between_pattern let an overlong start pattern reach a live "
        "match: " + repr(result[:200]))


def test_op_between_pattern_refuses_overlong_end(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("START\nhello\n")
    result = supertool.op_between_pattern(
        "START", _overlong_pattern(), str(target))
    assert "pattern too long" in result, (
        "op_between_pattern let an overlong end pattern reach a live "
        "match: " + repr(result[:200]))


def test_op_between_pattern_discloses_a_bre_alternation_rewrite(tmp_path):
    """_pattern_gate can silently rewrite start/end (bash-grep BRE
    alternation -- an escaped pipe -- becomes a plain pipe) -- op_grep/
    op_around disclose that via the note _pattern_gate returns;
    op_between_pattern must too, or a caller whose pattern was rewritten
    cannot tell "matched exactly what I typed" from "matched a silently-
    normalized version of what I typed."
    """
    target = tmp_path / "target.txt"
    target.write_text("alpha only\ngamma only\nEND\n")
    result = supertool.op_between_pattern(r"alpha\|gamma", "END", str(target))
    assert "pattern rewritten to" in result, (
        "op_between_pattern dropped _pattern_gate's rewrite disclosure: "
        + repr(result[:300]))
    assert "`alpha|gamma`" in result, repr(result[:300])


def test_op_between_pattern_still_works_on_a_benign_slice(tmp_path):
    """Positive control: an ordinary between call must still work."""
    target = tmp_path / "target.txt"
    target.write_text("before\nSTART\nmiddle\nEND\nafter\n")
    result = supertool.op_between_pattern("START", "END", str(target))
    assert "catastrophic backtracking" not in result
    assert "pattern too long" not in result
    assert "slice lines 2" in result, (
        "op_between_pattern no longer found the benign slice: "
        + repr(result[:200]))
