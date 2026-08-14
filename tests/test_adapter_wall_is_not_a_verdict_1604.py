"""A runner's wall is not a verdict about the file — the class, not one adapter (#1604).

Fifth instance of one defect: #1296 (`node --check`, 30s), #1360 (`xmllint`),
#1461 (`go vet`, 60s), #1501 (the core's own spawn wall), and now `cargo check`
at 120s on a loaded Windows runner:

    FAILED tests/test_cargo_sibling_attribution_754.py::test_real_crate_...
        AssertionError: cargo-check reported ok=False count=1 after 120000ms:
        [adapter] timeout (cargo check exceeded 120s)
        assert 'sibling.rs' in 'timeout (cargo check exceeded 120s)'

Each was fixed one adapter at a time and the machinery to fix them all has been
in `tests/_adapter_verdict.py` since #794: `stalled_at_its_own_wall` classifies a
payload that spent the adapter's whole internal budget without reaching a
verdict, and `skip_if_stalled` turns one into a **counted** decline rather than a
statement about the file. This file pins the two things that were stopping that
machinery from generalising.

## 1. One adapter's stall was unclassifiable, and it looked like a cosmetic bug

The same run that produced the cargo failure produced this one:

    FAILED tests/test_ruby_check.py::test_valid_ruby
        ruby-check reported ok=False count=1 after 0ms: [adapter] Command
        '['ruby', '-c', '...good.rb']' timed out after 30 seconds

`after 0ms` beside "timed out after 30 seconds" reads as a rendering nit. It is
not. `ruby-check.py` was the only adapter in `validators/` that spawned its tool
with a `timeout=` and had **no `except subprocess.TimeoutExpired` arm** — the
`TimeoutExpired` escaped `main()` into the module-level `except Exception`, whose
payload is hardcoded `"duration_ms": 0`.

`stalled_at_its_own_wall`'s fourth clause is `duration_ms >= inner_s * 1000`, and
it is right to be there: "an adapter that reports `timeout` in 12ms did not time
out, and its error routing is broken". So the guard read this payload correctly —
the error routing *was* broken — and refused to classify a real 30-second wall as
one. Fixing the call site alone would have left `test_valid_ruby` red on exactly
the leg the fix exists for. The `0ms` was the blocker, not a footnote.

## 2. The class guard could not see an adapter that handled nothing

`tests/test_pyright_timeout_is_an_adapter_fault_1464.py` sweeps every adapter's
`except subprocess.TimeoutExpired` arm and refuses any error code but `adapter`.
It reads handlers that exist, so the one adapter with no handler at all passed it
vacuously — an absence produced by the checker, read as an absence in the world,
which is this repo's own defect class inside the thing meant to detect it. The
sweep below asks the complementary question: does every adapter that grants
itself a wall have somewhere for that wall to land?

## 3. A broken adapter must not land in the same arm as a slow runner

Asked explicitly by #1604 and answered here rather than assumed: it does not.
`stalled_at_its_own_wall` needs all four clauses, and a crash inside the adapter
satisfies neither the wall phrase nor the duration floor, so it stays a red.
"""
from __future__ import annotations

import ast
import json
import runpy
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _adapter_budget import inner_budget  # noqa: E402
from _adapter_verdict import stalled_at_its_own_wall  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
VALIDATORS = REPO / "validators"
RUBY = VALIDATORS / "ruby-check" / "ruby-check.py"

RUBY_INNER_S = 30


def _drive(adapter: Path, tool: str, target: Path, monkeypatch, *,
           raise_exc: BaseException, elapsed_s: float) -> dict:
    """Run the real adapter, entry point and all, with its spawn raising.

    `runpy.run_path(..., run_name="__main__")` and not
    `spec.loader.exec_module` + `mod.main()`, because the difference between
    those two is the whole subject: every adapter's last-resort
    `except Exception` lives in the `if __name__ == "__main__"` guard, so
    calling `main()` directly runs an entry point CI never uses and makes an
    escaping exception look like no output at all. Whether a fault reaches that
    guard, and what it reports when it does, is exactly what is under test.

    In process, so it costs milliseconds instead of the adapter's real budget
    and it runs on Windows where a PATH shim does not. The patches land on the
    stdlib modules themselves rather than on a module object, because
    `run_path` re-executes the adapter's own imports.

    The clock is driven rather than slept: the question is what the adapter
    *reports* it spent, so the elapsed time has to be a fact the test controls
    and not one the runner supplies.
    """
    def _boom(*_args, **_kwargs):
        raise raise_exc

    ticks = iter([1000.0, 1000.0 + elapsed_s])
    last = [1000.0 + elapsed_s]

    def _clock():
        try:
            last[0] = next(ticks)
        except StopIteration:
            pass
        return last[0]

    emitted: list = []

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(time, "time", _clock)
    monkeypatch.setattr(shutil, "which", lambda _n: "/usr/bin/" + tool)
    monkeypatch.setattr(sys, "argv", [str(adapter), str(target)])
    monkeypatch.setattr("builtins.print",
                        lambda *a, **k: emitted.append(" ".join(map(str, a))))
    try:
        runpy.run_path(str(adapter), run_name="__main__")
    finally:
        monkeypatch.undo()

    payloads = [json.loads(line) for line in emitted if line.strip().startswith("{")]
    assert payloads, "the adapter emitted no JSON when its spawn failed"
    return payloads[-1]


@pytest.fixture()
def stalled_ruby(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    target = tmp_path / "good.rb"
    target.write_text('puts "hello"\n', encoding="utf-8")
    return _drive(
        RUBY, "ruby", target, monkeypatch,
        raise_exc=subprocess.TimeoutExpired(cmd=["ruby", "-c", str(target)],
                                            timeout=RUBY_INNER_S),
        elapsed_s=RUBY_INNER_S + 1.5)


# ---------------------------------------------------------------------------
# 1. the adapter reports the time it actually spent
# ---------------------------------------------------------------------------

def test_a_stalled_ruby_check_reports_the_time_it_actually_spent(
        stalled_ruby: dict) -> None:
    """`after 0ms` on a spawn that burned 30 seconds. The number is not decor:
    it is the one clause separating a wall from an adapter whose error routing
    is broken, and reporting 0 asserts the second about the first."""
    assert stalled_ruby["duration_ms"] >= RUBY_INNER_S * 1000, stalled_ruby


def test_a_stalled_ruby_check_declines_with_the_reserved_code(
        stalled_ruby: dict) -> None:
    """SCHEMA.md reserves `adapter` for "no verdict was obtained"; the core
    routes cache, rollback and NOT CHECKED off that one word (#1464)."""
    assert stalled_ruby["ok"] is False, stalled_ruby
    assert stalled_ruby["errors"][0]["code"] == "adapter", stalled_ruby
    assert "timed out" in stalled_ruby["errors"][0]["msg"].lower(), stalled_ruby


def test_a_stalled_ruby_check_is_classifiable_as_a_stall(
        stalled_ruby: dict) -> None:
    """The whole point. Until this holds, no call site in the ruby suite can
    decline a wall no matter how it is written."""
    assert stalled_at_its_own_wall(stalled_ruby, inner_s=RUBY_INNER_S) is not None, \
        stalled_ruby


# ---------------------------------------------------------------------------
# 3. a broken adapter is NOT the same arm
# ---------------------------------------------------------------------------

def test_a_ruby_check_that_crashed_is_not_read_as_a_slow_runner(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#1604 asks whether a genuinely broken adapter reaches the same arm as a
    timeout. It does not, and the answer must be pinned rather than assumed:
    a decline that swallows a real fault is the loud bug traded for the quiet
    one."""
    target = tmp_path / "good.rb"
    target.write_text('puts "hello"\n', encoding="utf-8")
    broken = _drive(RUBY, "ruby", target, monkeypatch,
                    raise_exc=RuntimeError("adapter is broken"), elapsed_s=99.0)
    assert broken["errors"][0]["code"] == "adapter", broken
    assert stalled_at_its_own_wall(broken, inner_s=RUBY_INNER_S) is None, broken


def test_a_wall_reported_impossibly_fast_is_still_not_a_stall() -> None:
    """The fourth clause, stated directly. This is what caught the ruby payload
    and it must keep catching the next one."""
    payload = {"tool": "x", "ok": False, "count": 1, "duration_ms": 12,
               "errors": [{"code": "adapter", "msg": "timed out after 30 seconds"}]}
    assert stalled_at_its_own_wall(payload, inner_s=30) is None, payload


# ---------------------------------------------------------------------------
# 2. the class: every adapter that grants itself a wall must catch it
# ---------------------------------------------------------------------------

def _spawns_with_a_budget(tree: ast.AST) -> list:
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = ast.unparse(node.func)
        if not (func.endswith("subprocess.run")
                or func.endswith("subprocess.check_output")
                or func.endswith("Popen")):
            continue
        if any(kw.arg == "timeout" for kw in node.keywords):
            found.append(node.lineno)
    return found


def _handles_its_own_wall(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or node.type is None:
            continue
        names = {n.attr for n in ast.walk(node.type) if isinstance(n, ast.Attribute)}
        names |= {n.id for n in ast.walk(node.type) if isinstance(n, ast.Name)}
        if "TimeoutExpired" in names:
            return True
    return False


def test_every_adapter_that_grants_itself_a_wall_catches_its_own_wall() -> None:
    """The complement of #1464's sweep, which reads handlers that exist.

    An adapter with no handler at all has no arm for that sweep to inspect, so
    it passed — and the one adapter in that state emitted a hardcoded
    `duration_ms: 0` from the module-level `except Exception`, which is
    precisely the shape `stalled_at_its_own_wall` refuses to classify. A
    checker whose zero means "I did not look" is this repo's defect class; here
    it was sitting inside the guard written against it.
    """
    offenders = []
    checked = 0
    for adapter in sorted(VALIDATORS.glob("*/*.py")):
        tree = ast.parse(adapter.read_text(encoding="utf-8", errors="replace"))
        budgeted = _spawns_with_a_budget(tree)
        if not budgeted:
            continue
        checked += 1
        if not _handles_its_own_wall(tree):
            offenders.append(
                adapter.relative_to(REPO).as_posix()
                + " spawns with timeout= at line(s) "
                + ", ".join(str(n) for n in budgeted)
                + " and has no `except subprocess.TimeoutExpired` arm")
    assert checked >= 20, (
        "only " + str(checked) + " adapters were found to spawn with a budget — "
        "the sweep is matching nothing and would pass vacuously")
    assert not offenders, (
        "an adapter's wall must land in an arm that reports the elapsed time it "
        "actually spent, or the suite cannot tell a loaded runner from an "
        "adapter whose error routing is broken (#1604):\n  "
        + "\n  ".join(offenders))


# ---------------------------------------------------------------------------
# the two call sites #1604 names, driven rather than grepped
# ---------------------------------------------------------------------------

def _stall_process(tool: str, inner_s: int, msg: str):
    payload = {"tool": tool, "file": "x", "ok": False, "count": 1,
               "duration_ms": inner_s * 1000,
               "errors": [{"line": None, "col": None, "severity": "error",
                           "code": "adapter", "msg": msg}]}
    return subprocess.CompletedProcess(args=[], returncode=0,
                                       stdout=json.dumps(payload), stderr="")


@pytest.mark.parametrize("module_name, tool, msg", [
    ("test_cargo_sibling_attribution_754", "cargo-check",
     "timeout (cargo check exceeded 120s)"),
    ("test_ruby_check", "ruby-check",
     "timed out after 30 seconds"),
])
def test_the_named_call_sites_decline_a_wall_instead_of_asserting_on_it(
        module_name: str, tool: str, msg: str,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Driven through each module's own spawn helper, not read off its source.

    A lexical check for `skip_if_stalled` would pass on an import nobody calls.
    This hands the helper the exact payload the loaded Windows runner produced
    and asserts the outcome is a decline — `pytest.skip` raises `Skipped`, so
    the assertion is that the helper never returns.
    """
    module = __import__(module_name)
    inner_s = inner_budget(module.ADAPTER)
    monkeypatch.setattr(module.subprocess, "run",
                        lambda *a, **k: _stall_process(tool, inner_s, msg))
    with pytest.raises(pytest.skip.Exception) as excinfo:
        module._run("subject")
    assert "internal budget" in str(excinfo.value), excinfo.value
