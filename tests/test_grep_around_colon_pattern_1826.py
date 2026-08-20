"""#1826 -- `grep_around` keeps its PATTERN and PATH in fixed slots, so a
colon-bearing pattern lands its tail in the N slot and used to surface as the
`int()` exception text.

The parse is deliberately NOT changed to the `_parse_grep_args` rejoin. Three
shipped guards depend on grep_around's slots being fixed:
`_PATH_ARG_POSITIONS["grep_around"] = (2,)` gates the PATH statically,
`_GREP_AROUND_ALL_IN_N_SLOT` reads `all` out of the N slot, and the fifth-token
refusal (#1345) counts slots. A rejoin peels trailing ints from the right and
would take `all` for the PATH, deleting the first two. So the fix is the third
option the issue names: a refusal that discloses how the call was read and
points at `grep_around:@-`, the payload route that already exists and is pinned
by `tests/test_read_op_colon_escape_625.py::TestGrepAroundAtPayload`.
"""

from __future__ import annotations

from pathlib import Path

import supertool


def _hay(tmp_path: Path) -> Path:
    f = tmp_path / "code.py"
    f.write_text("alpha\nClass::CONST = 1\nbeta\n", encoding="utf-8")
    return f


def test_colon_pattern_is_refused_and_names_the_payload_route(tmp_path: Path) -> None:
    """The reachable shape: `grep_around:Class::CONST:PATH`. parts[3] is the
    tail of the pattern, and `int()` used to raise through it."""
    f = _hay(tmp_path)
    out = supertool.dispatch(f"grep_around:Class::CONST:{f}")
    assert "ERROR" in out, out
    assert "invalid literal for int" not in out, out
    assert "grep_around:@-" in out, out
    assert "pattern" in out and "path" in out, out


def test_the_refusal_discloses_how_the_call_was_read(tmp_path: Path) -> None:
    """#1065's disclosure, which this op could never fire: parts[1] cannot hold
    a colon, so `_colon_split_hint` is unreachable here. The refusal carries it
    instead -- what the op took as PATTERN and what it took as PATH."""
    f = _hay(tmp_path)
    out = supertool.dispatch(f"grep_around:Class::CONST:{f}")
    assert "'Class'" in out, out
    assert "CONST" in out, out


def test_a_non_numeric_limit_slot_is_refused_the_same_way(tmp_path: Path) -> None:
    """The LIMIT slot raised through `int()` too -- same class, one slot over."""
    f = _hay(tmp_path)
    out = supertool.dispatch(f"grep_around:alpha:{f}:2:zz")
    assert "ERROR" in out, out
    assert "invalid literal for int" not in out, out
    assert "zz" in out, out


def test_the_payload_route_the_refusal_names_actually_answers(tmp_path: Path) -> None:
    """A refusal naming an escape that does not work is worse than the raw
    exception. This runs the escape."""
    f = _hay(tmp_path)
    spec = tmp_path / "p.json"
    spec.write_text(
        '{"pattern": "Class::CONST", "path": ' + repr(str(f)).replace("'", '"')
        + ', "n": 1, "limit": 5}', encoding="utf-8")
    out = supertool.dispatch(f"grep_around:@{spec}")
    assert "Class::CONST = 1" in out, out


def test_working_calls_still_work(tmp_path: Path) -> None:
    """The must-fire control's opposite: a guard that refuses every call would
    pass every assertion above. All four slots, all numeric."""
    f = _hay(tmp_path)
    out = supertool.dispatch(f"grep_around:alpha:{f}:1:5")
    assert "ERROR" not in out, out
    assert "alpha" in out, out


def test_all_is_still_a_limit(tmp_path: Path) -> None:
    """`all` is a LIMIT and only a LIMIT (#1328), and the new refusal nearly
    swallowed the shipped spelling: it is not an integer either. Caught by
    `test_grep_around_takes_all_too` on the full suite while #1826 was being
    written -- the same "a guard deleted a guard" failure the refusal's own
    docstring argues a right-to-left rejoin would cause, one slot over."""
    f = _hay(tmp_path)
    out = supertool.dispatch(f"grep_around:alpha:{f}:1:all")
    assert "ERROR" not in out, out
    assert "alpha" in out, out


def test_all_in_the_n_slot_keeps_its_own_refusal(tmp_path: Path) -> None:
    """#1328's message is more specific than the new one and must not be
    shadowed by it -- `all` is also not an int."""
    f = _hay(tmp_path)
    out = supertool.dispatch(f"grep_around:alpha:{f}:all")
    assert "context first" in out, out
