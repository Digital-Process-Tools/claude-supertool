"""#2734 -- every `SUPERTOOL_<OP>_<KEY>` override is read by its literal name.

`_get_op_int`/`_get_op_bool` used to assemble the variable's name at run
time and read `os.environ.get(env_key)`. The directory's validator cites
that shape ("an environment variable named at run time"), one call site per
scan, and an allowlist of "genuinely needed" by-name readers did not survive
the portal (sixth pass). `_OP_ENV_OVERRIDES` now spells every name out.

The table is only safe if it is complete, so this derives every `(op, key)`
the shipped core passes, from the AST rather than from a list typed here:
a new knob with no entry is red in CI, not an override that silently does
nothing. `_op_env_override` raising on a missing entry is the runtime half.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import supertool  # noqa: E402
from _core_sources import core_source_paths  # noqa: E402


def _pairs_from_ast() -> set:
    """(op, key) for every literal `_get_op_int/_get_op_bool(op, key, ...)`,
    plus every op name `_cap_context_window(text, "op")` forwards with the
    fixed key `max_bytes` -- the one caller that passes `op_name` through."""
    pairs: set = set()
    for path in core_source_paths():
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            name, args = node.func.id, node.args
            if name in ("_get_op_int", "_get_op_bool") and len(args) >= 2:
                if all(isinstance(a, ast.Constant) for a in args[:2]):
                    pairs.add((args[0].value, args[1].value))
            elif name == "_cap_context_window" and len(args) >= 2:
                if isinstance(args[1], ast.Constant):
                    pairs.add((args[1].value, "max_bytes"))
    return pairs


def test_ast_finds_the_known_knobs() -> None:
    """Positive control: a scan that found nothing would pass the subset
    check below vacuously."""
    pairs = _pairs_from_ast()
    assert ("read", "max_lines") in pairs
    assert ("grep", "count_truncated") in pairs
    assert ("around", "max_bytes") in pairs
    assert len(pairs) >= 15, pairs


def test_every_op_knob_the_core_reads_has_a_literal_reader() -> None:
    missing = _pairs_from_ast() - set(supertool._OP_ENV_OVERRIDES)
    assert not missing, (
        f"_get_op_int/_get_op_bool is called for {sorted(missing)} with no "
        "entry in _OP_ENV_OVERRIDES -- the override would raise at run time")


@pytest.mark.parametrize("pair", sorted(supertool._OP_ENV_OVERRIDES))
def test_each_reader_reads_the_name_the_docs_promise(pair, monkeypatch) -> None:
    """The literal in each lambda must be exactly SUPERTOOL_<OP>_<KEY>: a
    typo there would make a documented override silently inert."""
    op, key = pair
    expected = f"SUPERTOOL_{op.upper()}_{key.upper()}"
    monkeypatch.setenv(expected, "sentinel-2734")
    name, value = supertool._op_env_override(op, key)
    assert name == expected
    assert value == "sentinel-2734"


def test_an_unlisted_knob_raises_rather_than_reading_as_unset() -> None:
    with pytest.raises(KeyError, match="_OP_ENV_OVERRIDES"):
        supertool._op_env_override("read", "no_such_knob_2734")
