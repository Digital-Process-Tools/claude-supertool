"""#2573: `op_vim`'s search/substitute motions bypass `_pattern_gate` --
unbounded regex hang, no length cap, identical shape to #2571's fix for
`op_between_pattern`.

`_pattern_gate` (`_supertool.py`) is the single chokepoint the #150 ReDoS
backtracking guard (`_has_outer_wrapped_unbounded_group`, #2547) and the
1000-char length cap (#2550) both live behind. `op_between_pattern` reached
it in #2571; `op_vim`'s own search motions (`/`, `?`, `n`/`N`, the inline
`o`/`O` search-then-open reflex, `:s`, `:g`/`:v`, `:d /PAT/` address
resolution, and the operator motion forms `d/PAT`, `c/PAT`, `d?PAT`,
`c?PAT`) never did: every one of them compiled a caller-supplied pattern
straight with `re.compile` and searched live file content with it.

Fixed by gating each entry point's raw pattern through `_pattern_gate`
once, before the retry/autocorrect ladder that follows it, rather than at
every retry (`_pattern_gate` already normalises/refuses once; gating each
retry separately would just re-run the same check on a string that did not
change).

Patterns are assembled from separate string parts rather than written as
one literal, mirroring `tests/test_pattern_gate_between_2571.py`: CodeQL
flags the equivalent literal as a reachable "Inefficient regular
expression" sink on PR review, because it cannot see that the guard's own
early return is what stops `re.compile` from ever running on it.
"""
from __future__ import annotations

import supertool


def _adversarial_pattern() -> str:
    outer_prefix, inner_group, outer_suffix = "(x(", "a+", ")+y)"
    return outer_prefix + inner_group + outer_suffix


def _overlong_pattern() -> str:
    return "a" * 5000


def test_forward_search_refuses_adversarial_pattern(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaay\nEND\n")
    result = supertool.op_vim(str(f), "/" + _adversarial_pattern())
    assert "catastrophic backtracking" in result, (
        "op_vim let an adversarial / pattern reach a live re.compile/search: "
        + repr(result[:300]))


def test_forward_search_refuses_overlong_pattern(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("hello\nEND\n")
    result = supertool.op_vim(str(f), "/" + _overlong_pattern())
    assert "pattern too long" in result, repr(result[:300])


def test_backward_search_refuses_adversarial_pattern(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("START\nxaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaay\n")
    result = supertool.op_vim(str(f), "G␞?" + _adversarial_pattern())
    assert "catastrophic backtracking" in result, repr(result[:300])


def test_ex_delete_pattern_address_refuses_adversarial_pattern(tmp_path):
    """`_resolve_d`'s `/PAT/` line-address form (`:.,/PAT/d`) -- a third,
    independent raw-`re.compile` route through the same `:d` verb as the
    `:g/PAT/d` global-delete form covered above."""
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nxaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaay\nafter\n")
    result = supertool.op_vim(
        str(f), ":.,/" + _adversarial_pattern() + "/d")
    assert "catastrophic backtracking" in result, repr(result[:300])


def test_ex_substitute_refuses_adversarial_pattern(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaay\n")
    result = supertool.op_vim(
        str(f), ":s/" + _adversarial_pattern() + "/REPL/")
    assert "catastrophic backtracking" in result, repr(result[:300])


def test_ex_substitute_refuses_overlong_pattern(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("hello\n")
    result = supertool.op_vim(
        str(f), ":s/" + _overlong_pattern() + "/REPL/")
    assert "pattern too long" in result, repr(result[:300])


def test_global_delete_refuses_adversarial_pattern(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaay\nEND\n")
    result = supertool.op_vim(str(f), ":g/" + _adversarial_pattern() + "/d")
    assert "catastrophic backtracking" in result, repr(result[:300])


def test_operator_motion_forward_refuses_adversarial_pattern(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaay\nEND\n")
    result = supertool.op_vim(str(f), "d/" + _adversarial_pattern())
    assert "catastrophic backtracking" in result, repr(result[:300])


def test_operator_motion_backward_refuses_adversarial_pattern(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("START\nxaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaay\n")
    result = supertool.op_vim(str(f), "G␞?" + _adversarial_pattern())
    assert "catastrophic backtracking" in result, repr(result[:300])


def test_forward_search_still_works_on_a_benign_pattern(tmp_path):
    """Positive control: an ordinary / search must still work."""
    f = tmp_path / "x.txt"
    f.write_text("before\nNEEDLE\nafter\n")
    result = supertool.op_vim(str(f), "/NEEDLE")
    assert "catastrophic backtracking" not in result
    assert "pattern too long" not in result
    assert not result.startswith("ERROR"), repr(result[:300])


def test_ex_substitute_still_works_on_a_benign_pattern(tmp_path):
    """Positive control: an ordinary :s call must still work."""
    f = tmp_path / "x.txt"
    f.write_text("hello world\n")
    result = supertool.op_vim(str(f), ":s/world/there/")
    assert "catastrophic backtracking" not in result
    assert "pattern too long" not in result
    assert not result.startswith("ERROR"), repr(result[:300])
    assert f.read_text(encoding="utf-8") == "hello there\n"
