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
    """`d?PAT` is a single tokenized action (`_supertool.py`'s operator-
    motion `?` branch, ~18570) -- NOT the same code path as a bare `?PAT`
    search action, which the parser tokenizes as the plain `?` verb instead.
    `"G␞?" + pat` (as a previous revision of this test used) exercises
    only the plain `?` verb, already covered by
    `test_backward_search_refuses_adversarial_pattern` above, and would
    still pass with the operator-motion `?` branch's own gate deleted --
    caught in review (#2573)."""
    f = tmp_path / "x.txt"
    f.write_text("xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaay\nEND\n")
    result = supertool.op_vim(str(f), "d?" + _adversarial_pattern())
    assert "catastrophic backtracking" in result, repr(result[:300])


def test_repeat_search_n_still_works_after_priming(tmp_path):
    """Positive control for `n`'s own `_pattern_gate` call
    (`_supertool.py` ~17148): `last_search` is a script-local variable, not
    persisted across separate `op_vim` calls (`_op_vim_impl` re-initialises
    it to `None` every call), and the only way to populate it is through one
    of the already-gated `/`, `?`, `o`/`O` or operator-motion entry points --
    every one of which refuses an adversarial pattern before it is ever
    stored. So `n` can never independently be handed an adversarial pattern
    through ordinary use; this proves its own added gate call does not
    break the ordinary case, primed within a single multi-action script
    that shares one `last_search`."""
    f = tmp_path / "x.txt"
    f.write_text("needle here\nneedle there\n")
    result = supertool.op_vim(str(f), "/needle␞n")
    assert "catastrophic backtracking" not in result
    assert not result.startswith("ERROR"), repr(result[:300])


def test_inline_search_then_open_reflex_refuses_adversarial_pattern(tmp_path):
    """The `o?PAT`/`o/PAT` search-then-open autocorrect reflex
    (`_supertool.py` ~16410-16436) has its own `_pattern_gate` call,
    independent of the plain `/`/`?` verbs -- `oPAT` with no space and no
    other content is what routes here rather than through open-then-insert."""
    f = tmp_path / "x.txt"
    f.write_text("xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaay\nEND\n")
    result = supertool.op_vim(str(f), "o/" + _adversarial_pattern())
    assert "catastrophic backtracking" in result, repr(result[:300])


def test_ex_substitute_refuses_saturating_literal_pipe(tmp_path):
    """A saturating pattern is not a harmless single-match position for
    `:s` the way it is for `/`/`?`: `:s` rewrites EVERY match. A bare `|`
    (empty alternation on both sides) previously silently interleaved the
    replacement between every character of the buffer -- caught in review
    (#2573) after the first cut of this fix turned `check_saturation` off
    uniformly for every op_vim entry point, including this write-class one."""
    f = tmp_path / "x.txt"
    f.write_text("hello\n")
    result = supertool.op_vim(str(f), ":s/|/X/g")
    assert "saturated pattern" in result or "matches every line" in result, (
        repr(result[:300]))
    assert f.read_text(encoding="utf-8") == "hello\n", (
        "a saturating :s pattern silently rewrote the buffer instead of "
        "being refused: " + repr(f.read_text(encoding='utf-8')))


def test_global_delete_refuses_saturating_literal_pipe(tmp_path):
    """Same shape as the `:s` case above, for `:g`/`:v`: a bare `|` as PAT
    previously deleted every line in the file silently."""
    f = tmp_path / "x.txt"
    f.write_text("alpha\nbeta\ngamma\n")
    result = supertool.op_vim(str(f), ":g/|/d")
    assert "saturated pattern" in result or "matches every line" in result, (
        repr(result[:300]))
    assert f.read_text(encoding="utf-8") == "alpha\nbeta\ngamma\n", (
        "a saturating :g pattern silently deleted the whole file instead "
        "of being refused: " + repr(f.read_text(encoding='utf-8')))


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
