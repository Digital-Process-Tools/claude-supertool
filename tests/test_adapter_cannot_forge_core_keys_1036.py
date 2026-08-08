"""An adapter that claims a timeout switched off `rollback_on_fail` (#1036).

#969 gave the core a third state: a checker that could not answer must not roll
back an edit it never checked. `_validator_no_verdict` reads that state off two
keys — `no_verdict`, and the `timeout` flag `_validator_run_one`'s
`TimeoutExpired` arm stamps on its own synthetic result.

`_validator_run_one` returned the adapter's parsed JSON with only `elapsed_s`
and `resolved_to` added, and nothing removed. So an adapter could put
`"timeout": true` next to a real finding and declare itself a non-verdict: the
row printed `NOT CHECKED`, the guard did not run, and a bad edit stood on a
validator configured to revert it. `SCHEMA.md` forbade `no_verdict` to adapters
and said nothing about `timeout`; neither prohibition was enforced anywhere.

**The core's timeout and an adapter's claim of one are different facts, and
only the first is evidence.** The boundary is therefore a chokepoint rather than
a rule about one key: every key the core sets on a result is dropped from the
adapter's payload before that payload reaches any decision, and the adapter's
own verdict (`ok`, `count`, `errors`) is left exactly as it was. Ignoring the
forged key rather than refusing the whole result is deliberate — a refusal would
turn the result into a skip, which is *also* a non-verdict, and would hand a
buggy adapter the same rollback bypass through the other door.

The class, not the instance: `test_every_core_only_key_read_by_a_decision_is_
stripped` fails if any future key consulted by a core-only decision is not
registered at the chokepoint.
"""
from __future__ import annotations

import ast
import inspect
import json
import textwrap
from pathlib import Path

import pytest

import supertool

CLEAN = json.dumps({"tool": "fake", "ok": True, "count": 0, "errors": [],
                    "duration_ms": 1})
REAL_FINDING = {
    "tool": "fake", "ok": False, "count": 1,
    "errors": [{"line": 1, "col": 1, "severity": "error",
                "code": "E999", "msg": "unterminated object"}],
    "duration_ms": 1,
}

BEFORE = '{"a": 1}\n'
AFTER = '{"b": 1}\n'

# The keys SCHEMA.md declares an adapter may emit.
SCHEMA_ADAPTER_KEYS = frozenset({
    "tool", "file", "ok", "count", "errors", "duration_ms", "metrics", "diff",
    "skipped",
})

# The contract this file owns: keys the core stamps on a result and an adapter
# may never supply. The core registers the same set; `test_the_core_registers_
# the_same_boundary` pins the two together rather than letting this one drift
# into asserting whatever the core happens to say.
CORE_ONLY_KEYS = frozenset({"no_verdict", "timeout", "elapsed_s", "resolved_to"})


def _core_only() -> frozenset:
    return frozenset(getattr(supertool, "_VALIDATOR_CORE_ONLY_KEYS", ()))


def _strip(payload: dict) -> dict:
    return supertool._validator_strip_core_keys(dict(payload))


def _forging(**extra: object) -> str:
    return json.dumps({**REAL_FINDING, **extra})


def _two_pass_adapter(tmp_path: Path, first: str, second: str) -> str:
    """Answers `first` on the baseline pass and `second` on every pass after.

    Same shape as `test_rollback_no_verdict_969`: the baseline and the post-edit
    check are two separate spawns, and only a counter file survives between them.
    `{python}` + `as_posix()` so it spawns under `shell=False` everywhere.
    """
    state = tmp_path / "_calls.txt"
    script = tmp_path / "_adapter.py"
    script.write_text(
        "import pathlib, sys" + chr(10)
        + f"state = pathlib.Path({str(state)!r})" + chr(10)
        + "n = int(state.read_text()) if state.exists() else 0" + chr(10)
        + "state.write_text(str(n + 1))" + chr(10)
        + f"sys.stdout.write({first!r} if n == 0 else {second!r})" + chr(10),
        encoding="utf-8",
    )
    return f"{{python}} {script.as_posix()}"


def _configure(cmd: str, timeout: int = 30) -> None:
    supertool._CONFIG = {"validators": {
        "fake": {"cmd": cmd, "match": "*.json", "cache": False,
                 "rollback_on_fail": True,
                 "hooks_into": ["edit", "replace", "replace_lines", "paste",
                                "append", "vim"],
                 "timeout": timeout},
    }}
    supertool._CONFIG_CHECKED = True


def _edit(tmp_path: Path, capsys) -> "tuple[int, str, str]":
    f = tmp_path / "s.json"
    f.write_text(BEFORE, encoding="utf-8")
    rc = supertool.main([f'edit:::"a":::"b":::{f}'])
    return rc, capsys.readouterr().out, f.read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _stable_branch(monkeypatch):
    monkeypatch.setattr(supertool, "_branch_reading", lambda: ("f", ""))


# ---------------------------------------------------------------------------
# THE bug — the post-condition is the file's bytes
# ---------------------------------------------------------------------------

def test_a_forged_timeout_does_not_disable_the_rollback_guard(
        tmp_path: Path, capsys) -> None:
    """Clean baseline, then a real finding wearing `timeout: true`.

    On `v0.26.0` this rolled back; after #969 the same adapter kept the bad
    write. The adapter never timed out — the core is the only thing that can
    know whether it did.
    """
    _configure(_two_pass_adapter(tmp_path, CLEAN, _forging(timeout=True)))
    _rc, out, text = _edit(tmp_path, capsys)
    assert text == BEFORE, (
        f"an adapter turned off rollback_on_fail by claiming a timeout:"
        f"{chr(10)}{out}")
    assert "rolled back" in out, out


def test_a_forged_no_verdict_does_not_disable_the_rollback_guard(
        tmp_path: Path, capsys) -> None:
    """SCHEMA.md already forbade this key. Nothing enforced the prohibition."""
    _configure(_two_pass_adapter(tmp_path, CLEAN, _forging(no_verdict=True)))
    _rc, out, text = _edit(tmp_path, capsys)
    assert text == BEFORE, f"a forged no_verdict survived into the guard:{chr(10)}{out}"
    assert "rolled back" in out, out


@pytest.mark.parametrize("key", sorted(CORE_ONLY_KEYS))
def test_no_core_only_key_an_adapter_forges_can_save_a_bad_edit(
        key: str, tmp_path: Path, capsys) -> None:
    """The class. Every key the core owns, forged next to a real finding."""
    _configure(_two_pass_adapter(tmp_path, CLEAN, _forging(**{key: True})))
    _rc, out, text = _edit(tmp_path, capsys)
    assert text == BEFORE, (
        f"forging {key!r} suppressed the rollback:{chr(10)}{out}")


# ---------------------------------------------------------------------------
# The chokepoint itself
# ---------------------------------------------------------------------------

def test_the_core_registers_the_same_boundary() -> None:
    assert _core_only() == CORE_ONLY_KEYS


def test_the_core_strips_every_key_it_owns_from_an_adapter_payload() -> None:
    payload = {**REAL_FINDING, **{k: "forged" for k in CORE_ONLY_KEYS}}
    kept = _strip(payload)
    assert not (set(kept) & CORE_ONLY_KEYS), kept
    for k in SCHEMA_ADAPTER_KEYS & set(REAL_FINDING):
        assert kept[k] == REAL_FINDING[k], (
            f"stripping a core key altered the adapter's own {k!r}: {kept!r}")


def test_stripping_leaves_a_well_behaved_adapter_untouched() -> None:
    clean = json.loads(CLEAN)
    assert _strip(clean) == clean


def test_every_core_only_key_read_by_a_decision_is_stripped() -> None:
    """The class test that outlives this instance.

    Any key a core-only decision reads off a validator result is either a
    SCHEMA-declared adapter key or one the core stamps itself — and if it is the
    second, the chokepoint has to drop the adapter's copy of it. A key that is
    neither is a new `timeout`: a channel an adapter can write into that decides
    something only the core is entitled to decide.
    """
    decisions = (supertool._validator_no_verdict,
                 supertool._validator_regressed,
                 supertool._validator_baseline,
                 supertool._validator_gate_did_not_run,
                 supertool._validator_not_checked)
    result_vars = {"data", "after", "before", "result"}
    read: set[str] = set()
    for fn in decisions:
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        for node in ast.walk(tree):
            name = None
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in result_vars
                    and node.args and isinstance(node.args[0], ast.Constant)):
                name = node.args[0].value
            elif (isinstance(node, ast.Compare) and len(node.ops) == 1
                  and isinstance(node.ops[0], ast.In)
                  and isinstance(node.left, ast.Constant)
                  and isinstance(node.comparators[0], ast.Name)
                  and node.comparators[0].id in result_vars):
                name = node.left.value
            if isinstance(name, str):
                read.add(name)

    assert read, "the key scan found nothing — it stopped testing what it claims to"
    unowned = read - SCHEMA_ADAPTER_KEYS - _core_only()
    assert not unowned, (
        f"a core-only decision reads {sorted(unowned)}, which is neither a "
        f"SCHEMA.md adapter key nor registered in _VALIDATOR_CORE_ONLY_KEYS — "
        f"an adapter can set it and decide this for the core")


# ---------------------------------------------------------------------------
# Secondary: the provenance column
# ---------------------------------------------------------------------------

def test_an_adapters_own_message_never_prints_under_the_orchestrator_column() -> None:
    """The two provenance columns exist to keep the core's text apart from the
    adapter's. A forged `timeout` printed the adapter's sentence under
    `orchestrator` — the core quoted as saying something it never said."""
    forged = {"tool": "fake", "file": "s.json", "ok": False, "count": 1,
              "timeout": True, "elapsed_s": 0.1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "fake could not reach its tool"}]}
    rows = supertool._validator_render_diff(None, _strip(forged))
    body = chr(10).join(rows)
    assert "orchestrator" not in body, (
        f"an adapter's message was rendered as the core's:{chr(10)}{body}")
    assert "adapter" in body, body


def test_the_cores_own_timeout_still_prints_under_orchestrator() -> None:
    """The other direction: the core's synthetic result is not touched by the
    strip, because it never went through it."""
    rows = supertool._validator_render_diff(None, {
        "tool": "fake", "file": "s.json", "ok": False, "count": 1,
        "timeout": True, "elapsed_s": 10.0,
        "errors": [{"line": None, "col": None, "severity": "error",
                    "code": "orchestrator", "msg": "timeout after 10s"}]})
    body = chr(10).join(rows)
    assert "orchestrator" in body, body
    assert "timed out" in body, body
