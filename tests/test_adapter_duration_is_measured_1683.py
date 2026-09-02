"""#1683 -- an adapter's `duration_ms` on a failure path has to be measured.

`_adapter_verdict.stalled_at_its_own_wall`'s fourth clause is
`duration_ms >= inner_s * 1000`, and its stated job is to catch an adapter that
"reports `timeout` in 12ms" -- error routing that sent a fast failure down the
wall arm. #1604 is that clause working: `ruby-check`'s `TimeoutExpired` escaped
into a handler hardcoding `duration_ms: 0`, and a real 30-second wall was
refused classification.

**The clause is meaningful; the number is what made it unenforceable.**
Thirteen adapters wrote the budget itself into the timeout arm -- `30000`, `120000`,
`TIMEOUT_S * 1000` -- so the value the classifier reads was a constant the same
file wrote. That satisfies the floor by construction, and it satisfies it
*whatever happened*: an adapter whose routing sent a 12ms failure down the
timeout arm still prints `30000` and is still declined as a stall, which is
exactly the defect the clause exists to keep visible. So the repair is not a
different clause. It is to make each of those numbers an observation, and to
stop a literal coming back.

**The rule: the value has to resolve to a clock read.** Names are followed
(`dur = int((time.time() - start) * 1000)` is measured, and reporting `dur`
would be a false finding), and so are calls to functions defined in the same
file (`_ms(start)`, `ms()`). `TIMEOUT_S * 1000` resolves to an `int` literal
and is not. Naming `time` is a narrowness this file accepts rather than hides:
it is the stdlib clock, all four spellings in this tree route through it, and
an adapter that measured elapsed some other way would have to say so here. The
alternative -- "the expression contains any call" -- was tried first and
reported six false findings on `dur` and `duration`, which is a guard that
teaches people to route around it.

**Only `except` handlers are read.** An adapter's argv arm -- "no file arg", a
`BIN not found` before any clock starts -- legitimately reports `0`: nothing
ran, and demanding a number there would be demanding a fabricated one. Those
are ordinary `if` arms and stay out of scope. An `except` handler is always
downstream of work that took time, and is the one place a written-down
duration is read by a classifier as evidence.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VALIDATORS = REPO / "validators"
FORMATTERS = REPO / "formatters"

#: Attributes of `time` that return a clock reading.
CLOCKS = ("time", "monotonic", "perf_counter",
          "time_ns", "monotonic_ns", "perf_counter_ns")


def _returns(module):
    """`function name -> [returned expression, ...]` for every def in the file."""
    out: dict = {}
    for node in ast.walk(module):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        out.setdefault(node.name, []).extend(
            r.value for r in ast.walk(node)
            if isinstance(r, ast.Return) and r.value is not None)
    return out


def _assignments(container):
    """`name -> [bound expression, ...]` inside one scope, nested defs included."""
    out: dict = {}
    for node in ast.walk(container):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out.setdefault(target.id, []).append(node.value)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            if isinstance(node.target, ast.Name):
                out.setdefault(node.target.id, []).append(node.value)
    return out


def _reads_a_clock(expr, returns, binds, seen=(), depth=0) -> bool:
    """Does `expr`, followed through names and local functions, reach a clock?"""
    if depth > 12:
        return False
    for node in ast.walk(expr):
        if (isinstance(node, ast.Attribute) and node.attr in CLOCKS
                and isinstance(node.value, ast.Name) and node.value.id == "time"):
            return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node, ast.Name):
            name = node.id
        else:
            continue
        if name in seen:
            continue
        onward = seen + (name,)
        for candidate in list(returns.get(name, ())) + list(binds.get(name, ())):
            if _reads_a_clock(candidate, returns, binds, onward, depth + 1):
                return True
    return False


def _fabricated_durations(source: str):
    """`(lineno, caught exception, unparsed value)` per unmeasured duration."""
    module = ast.parse(source)
    returns = _returns(module)
    module_binds = _assignments(module)
    functions = [n for n in ast.walk(module)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    out = []
    for handler in (n for n in ast.walk(module)
                    if isinstance(n, ast.ExceptHandler)):
        enclosing = [f for f in functions
                     if f.lineno <= handler.lineno <= (f.end_lineno or f.lineno)]
        binds = dict(module_binds)
        for scope in sorted(enclosing, key=lambda f: f.lineno):
            binds.update(_assignments(scope))
        caught = ast.unparse(handler.type) if handler.type else "bare except"
        for node in ast.walk(handler):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if not (isinstance(key, ast.Constant)
                        and key.value == "duration_ms"):
                    continue
                if _reads_a_clock(value, returns, binds):
                    continue
                out.append((value.lineno, caught, ast.unparse(value)))
    return out


def _adapters():
    """Every `validators/*/*.py` -- the 36 adapters and the 4 shared helpers
    under `common/` -- plus every `formatters/*/*.py` (#2159). The helpers
    are swept rather than filtered out: one of them, `refusal.py`, builds the
    `skipped` and `absent` payloads several adapters emit, so a fabricated
    duration could be written there once and reach all of them. Formatters
    join for the same reason `guard_main`'s own sweep does: the clause this
    file tests reads a classifier consumes (`stalled_at_its_own_wall`), and a
    formatter's `duration_ms` is read by that classifier exactly like a
    validator's -- restricting the population to where #1683 happened to be
    measured, rather than to what the contract covers, would leave a fourth
    fabricated-timeout constant class sitting one directory over, unswept."""
    found = sorted(VALIDATORS.glob("*/*.py")) + sorted(FORMATTERS.glob("*/*.py"))
    assert found, "no files found -- the sweep below would pass vacuously"
    return found


def test_no_adapter_reports_a_duration_it_did_not_measure() -> None:
    """The sweep. One entry per fabricated number, with the arm it sits in."""
    offenders = {}
    for adapter in _adapters():
        rel = adapter.relative_to(REPO).as_posix()
        found = _fabricated_durations(adapter.read_text(encoding="utf-8"))
        if found:
            offenders[rel] = found
    assert not offenders, (
        "these `duration_ms` values are constants the adapter wrote rather "
        "than elapsed time it measured, on a path a classifier reads as "
        "evidence (#1683): " + repr(offenders))


def test_the_detector_tells_a_written_number_from_a_read_one() -> None:
    """Without this the sweep above is a claim about a walk nobody checked.

    Each source is a string, so none of these handlers is in this module's own
    AST -- the same construction as the register in
    `test_directory_removal_ownership_1635.py`, and for the same reason.
    """
    nl = chr(10)

    def found(*body):
        return _fabricated_durations(nl.join(body) + nl)

    assert found("try:",
                 "    run()",
                 "except TimeoutExpired:",
                 "    emit({'duration_ms': 30000})"), (
        "a literal budget in the timeout arm is the #1683 shape")

    assert found("TIMEOUT_S = 30",
                 "try:",
                 "    run()",
                 "except TimeoutExpired:",
                 "    emit({'duration_ms': TIMEOUT_S * 1000})"), (
        "hoisting the literal into a constant does not make it measured -- it "
        "is still the budget, not the elapsed")

    assert found("try:",
                 "    main()",
                 "except Exception:",
                 "    emit({'duration_ms': 0})"), (
        "the crash arm is #1683's second instance: `start` out of scope, so "
        "every crash reported 0ms whenever it fired")

    assert not found("import time",
                     "try:",
                     "    run()",
                     "except TimeoutExpired:",
                     "    emit({'duration_ms': int((time.time() - start) * 1000)})")

    assert not found("import time",
                     "def main():",
                     "    try:",
                     "        run()",
                     "    except FileNotFoundError:",
                     "        dur = int((time.time() - start) * 1000)",
                     "        emit({'duration_ms': dur})"), (
        "a name bound to a clock read is measured, and reporting it would be "
        "a false finding -- six adapters spell it this way")

    assert not found("import time",
                     "def _ms(start):",
                     "    return int((time.time() - start) * 1000)",
                     "try:",
                     "    run()",
                     "except Exception:",
                     "    emit({'duration_ms': _ms(start)})"), (
        "a helper defined in the same file is a measurement; the guard must "
        "not demand one spelling")

    assert not found("import time",
                     "try:",
                     "    run()",
                     "except Exception:",
                     "    emit({'duration_ms': int((time.monotonic() - t0) * 1000)})"), (
        "the MCP adapters read a different clock and are correct")

    assert not found("def main():",
                     "    if not sys.argv[1:]:",
                     "        emit({'duration_ms': 0})"), (
        "an argv arm before any clock starts has no elapsed to report")


def test_a_ruby_check_crash_reports_the_time_it_actually_took() -> None:
    """The AST sweep above is a shape check; this one runs the arm (#1683).

    `ruby-check` is the only adapter in the tree with a module-level
    `except Exception`, so it is the only place this is runnable. `main`'s
    `start` is a local and was never in that handler's scope, so the payload
    said `0ms` for a crash five seconds in as readily as for one at import.

    Driven through `runpy` with `subprocess.run` replaced rather than through
    a fixture binary: the crash has to come from inside `main` after time has
    passed, and no input to the real adapter produces one on demand.
    """
    adapter = VALIDATORS / "ruby-check" / "ruby-check.py"
    driver = chr(10).join((
        # The adapter's path arrives as argv rather than baked into this
        # source, so nothing here has to escape a Windows path separator.
        "import runpy, shutil, subprocess, sys, time",
        "adapter = sys.argv[1]",
        "def boom(*a, **k):",
        "    time.sleep(0.2)",
        "    raise RuntimeError('boom')",
        # `which` has to say yes or the adapter declines with `absent` and
        # never reaches the crash; `run` then raises from inside `main`.
        "subprocess.run = boom",
        "shutil.which = lambda name: '/nonexistent/' + name",
        "sys.argv = ['ruby-check.py', 'some.rb']",
        "runpy.run_path(adapter, run_name='__main__')",
    ))
    proc = subprocess.run(
        [sys.executable, "-c", driver, str(adapter)],
        capture_output=True, text=True, timeout=60,
        encoding="utf-8", errors="replace")
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["ok"] is False and "boom" in payload["errors"][0]["msg"], payload
    assert payload["duration_ms"] >= 100, (
        "the crash arm reported a duration below the 200ms the crash took, so "
        "it is not reading a clock: " + repr(payload))
