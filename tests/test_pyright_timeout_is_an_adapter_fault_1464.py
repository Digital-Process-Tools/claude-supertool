"""A stalled pyright was a finding about the file, not an absence (#1464).

`validators/pyright/pyright.py`'s `TimeoutExpired` arm emitted
`code: "timeout"`. Every core route that gives an adapter fault its third-state
treatment keys on the one reserved word `adapter` — `_NONDETERMINISTIC_ERROR_CODES`
(never cache it), `_validator_not_checked` (render `NOT CHECKED`, not a verdict),
and through that `_validator_regressed` (never roll back over it).

`timeout` matched none of them, so a checker that never reached a verdict was one
error on the file: cached until the file's hash changed, subtracted from a
baseline as if it were real, and — the reason this is `destroys` — counted as a
new finding by the rollback path, which reverts the user's edit with nothing to
restore it from.

Measured on master `2983d6a` before the fix, driving the real adapter and feeding
its own payload to the real core predicates:

    ADAPTER PAYLOAD: {"code": "timeout", ...}
    _validator_not_checked         -> None   (renders as a verdict)
    _validator_result_is_cacheable -> True   (a stall replays forever)
    _validator_regressed           -> True   (rolls the edit back)

SCHEMA.md §"`adapter`: the reserved code for 'no verdict was obtained'" already
names all four cases in words — "a binary that is absent, a timeout, output that
would not parse, a tool that exited non-zero without saying anything about the
file" — so this was a violation of a written contract, not a design gap.

**The failure is silent by construction**: the payload is well-formed, no code
path warns, and the only symptom is a fault presenting as a finding. So the
class is pinned too, mechanically over every adapter's timeout arm, rather than
trusting a reader to notice the next one.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

import supertool

REPO = Path(__file__).resolve().parent.parent
VALIDATORS = REPO / "validators"
PYRIGHT = VALIDATORS / "pyright" / "pyright.py"


def _drive_timeout(adapter: Path, tool: str, target: Path,
                   monkeypatch: pytest.MonkeyPatch) -> dict:
    """Run the real adapter with its spawn raising `TimeoutExpired`.

    In-process, same technique as `test_adapter_timeout_policy_702_658.py`: it
    costs milliseconds instead of the adapter's real 60s budget and it runs on
    Windows, where a PATH shim does not.
    """
    spec = importlib.util.spec_from_file_location("adapter_under_test", adapter)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def _always_times_out(*_args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=[tool], timeout=kwargs.get("timeout", 60))

    monkeypatch.setattr(mod.subprocess, "run", _always_times_out)
    monkeypatch.setattr(mod.shutil, "which", lambda _name: "/usr/bin/" + tool)
    monkeypatch.setattr(mod.sys, "argv", [str(adapter), str(target)])

    emitted: list = []
    monkeypatch.setattr("builtins.print",
                        lambda *a, **k: emitted.append(" ".join(map(str, a))))
    mod.main()
    payloads = [json.loads(line) for line in emitted if line.strip().startswith("{")]
    assert payloads, "the adapter emitted no JSON when its budget expired"
    return payloads[-1]


@pytest.fixture()
def stalled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    target = tmp_path / "subject.py"
    target.write_text("x: int = 1\n")
    return _drive_timeout(PYRIGHT, "pyright", target, monkeypatch)


def test_a_stalled_pyright_declines_with_the_reserved_code(stalled: dict) -> None:
    """The one-word fix, asserted on the adapter's own output."""
    assert stalled["errors"][0]["code"] == "adapter", stalled
    assert "timeout" in stalled["errors"][0]["msg"].lower(), stalled


def test_the_core_reads_a_stalled_pyright_as_no_verdict(stalled: dict) -> None:
    """`NOT CHECKED`, not `1 err` — the adapter's payload through the real core."""
    assert supertool._validator_not_checked(stalled) is not None, stalled


def test_a_stalled_pyright_is_never_cached(stalled: dict) -> None:
    """The cache key is a content hash; a stall is not a fact about the content."""
    assert supertool._validator_result_is_cacheable(stalled) is False, stalled


def test_a_stalled_pyright_cannot_roll_back_the_edit(stalled: dict) -> None:
    """The `destroys` half. `rollback_on_fail` must not revert over an absence."""
    clean = {"tool": "pyright", "ok": True, "count": 0, "errors": []}
    assert supertool._validator_regressed(clean, stalled) is False, stalled
    assert supertool._validator_regressed(None, stalled) is False, stalled


# ---------------------------------------------------------------------------
# The class, not the instance
# ---------------------------------------------------------------------------

def _timeout_arm_codes(adapter: Path) -> list:
    """Every literal `"code": "..."` inside an `except ...TimeoutExpired...` arm."""
    tree = ast.parse(adapter.read_text(encoding="utf-8", errors="replace"))
    found = []
    for handler in ast.walk(tree):
        if not isinstance(handler, ast.ExceptHandler) or handler.type is None:
            continue
        names = {n.attr for n in ast.walk(handler.type) if isinstance(n, ast.Attribute)}
        names |= {n.id for n in ast.walk(handler.type) if isinstance(n, ast.Name)}
        if "TimeoutExpired" not in names:
            continue
        for node in ast.walk(ast.Module(body=handler.body, type_ignores=[])):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if (isinstance(key, ast.Constant) and key.value == "code"
                        and isinstance(value, ast.Constant)
                        and isinstance(value.value, str)):
                    found.append((value.lineno, value.value))
    return found


def test_no_adapter_spells_a_timeout_with_a_code_the_core_does_not_route() -> None:
    """The silent-by-construction half: nothing else would ever say so.

    Only literal codes are read. An arm that routes through a helper
    (`_adapter_error`, `absent`, `skipped` — go-vet, ruff, shellcheck) has no
    literal here and is covered by that helper's own tests; there is no shape in
    which this sweep is the only check on such an arm.
    """
    offenders = []
    literals = 0
    for adapter in sorted(VALIDATORS.glob("*/*.py")):
        for lineno, code in _timeout_arm_codes(adapter):
            literals += 1
            if code != "adapter":
                offenders.append(
                    adapter.relative_to(REPO).as_posix() + ":" + str(lineno)
                    + " emits code=" + repr(code))
    assert literals >= 10, (
        "only " + str(literals) + " literal codes found in timeout arms — the "
        "sweep is matching nothing and would pass vacuously"
    )
    assert not offenders, (
        "a timeout is an absence, and SCHEMA.md reserves `adapter` for one. Any "
        "other code is read by the core as a finding about the file: cached, "
        "subtracted from a baseline, and able to roll back an edit (#1464):\n  "
        + "\n  ".join(offenders)
    )
