#!/usr/bin/env python3
"""ruby-check validator adapter — Ruby syntax check via `ruby -c`.

Requires ruby on PATH. Ships on most Mac/Linux by default. Absent, this reports
the third state — `skipped` with the reason — rather than the `ok: true` it
emitted until #1202, which was a clean verdict about a file nothing parsed. Name
this validator in `$SUPERTOOL_REQUIRE_VALIDATORS` to turn that absence into a
loud error instead.

Usage:  ruby-check.py <file>
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "common"))
from source_context import context_fields
from refusal import absent, tool_fault
from linebreaks import split_lines

TOOL = "ruby-check"
INSTALL_HINT = "ruby not found on PATH — this file was NOT syntax-checked"

# Hoisted out of the `subprocess.run` call so the decline below can name the
# number, and so `tests/_adapter_budget.inner_budget` reads one value rather
# than two spellings of it.
TIMEOUT_S = 30

#: `main`'s own `start`, published for the module-level crash handler at the
#: foot of this file. `start` is a local and was never in that handler's scope,
#: so every crash reported `0ms` however long it had run -- including one five
#: seconds in (#1683).
#:
#: The same read every other arm reports against, not a second `time.time()`
#: at import: a separate clock would make the crash arm's number disagree with
#: its siblings', and it would consume a tick the driven-clock fixtures in
#: `tests/test_adapter_wall_is_not_a_verdict_1604.py` hand to `start`.
_STARTED: list = []


# `file:line: message`. Extracted so the rule can be driven in process on every
# platform — the fake-binary fixture is POSIX-only (see
# tests/test_adapter_tool_vs_file_753.py).
DIAGNOSTIC = re.compile(r"^(?:.*?):(\d+):\s+(.+)$")
SUMMARY = re.compile(r"^\d+\s+error")


def parse_diagnostics(out: str, file: str) -> list[dict]:
    """Every located diagnostic in ruby's stderr, minus its own count summary.

    Empty means ruby exited non-zero without placing anything in the file —
    `ruby: No such file or directory -- x.rb (LoadError)` is the common shape.
    """
    errors = []
    for line in split_lines(out):
        m = DIAGNOSTIC.match(line)
        if m:
            lineno, msg = m.groups()
            if SUMMARY.match(msg):
                continue
            ln = int(lineno)
            errors.append({"line": ln, "col": None, "severity": "error",
                           "code": "syntax", "msg": msg.strip()[:300],
                           **context_fields(file, ln)})
    return errors


def emit(d: dict) -> None:
    print(json.dumps(d))


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": "ruby-check", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return

    file = sys.argv[1]
    start = time.time()
    _STARTED[:] = [start]

    if not shutil.which("ruby"):
        emit(absent(TOOL, file, INSTALL_HINT,
                    int((time.time() - start) * 1000)))
        return

    try:
        result = subprocess.run(
            ["ruby", "-c", file],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        # `which` said yes and exec said no — a PATH entry that vanished
        # between the two, or a name that resolves to something unrunnable.
        # Still an absent tool, so still the third state.
        emit(absent(TOOL, file, "ruby on PATH but could not be executed — "
                                "this file was NOT syntax-checked",
                    int((time.time() - start) * 1000)))
        return
    except subprocess.TimeoutExpired:
        # Until #1604 this arm did not exist and the `TimeoutExpired` escaped
        # `main()` into the module-level `except Exception` below, which
        # hardcoded `duration_ms: 0` until #1683 gave it `_STARTED` to measure
        # against. The code was right and the number was a
        # lie, and the number is what the suite reads: a stall is only
        # distinguishable from an adapter with broken error routing by whether
        # the elapsed time reaches the budget, so `after 0ms` beside "timed out
        # after 30 seconds" made a real wall unclassifiable and published it as
        # a verdict about the file (tests/_adapter_verdict.py,
        # `stalled_at_its_own_wall`, fourth clause).
        emit({"tool": TOOL, "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter",
                          "msg": f"timed out (ruby -c exceeded {TIMEOUT_S}s)"}],
              "duration_ms": int((time.time() - start) * 1000)})
        return

    duration = int((time.time() - start) * 1000)

    if result.returncode == 0:
        emit({"tool": "ruby-check", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": duration})
        return

    # Parse ruby -c stderr: "file:line: message"
    output = result.stderr.strip()
    errors = parse_diagnostics(output, file)

    if not errors:
        # `ruby -c` exited non-zero without a `file:line:` diagnostic. That is
        # not "no syntax errors" (returncode == 0 already handled that above) —
        # resolving on PATH is not proof the spawned process ran like ruby (a
        # shim, alias, or broken install can exit non-zero silently). An
        # unexplained exit must stay a named error rather than fold into
        # `count: 0`, which reads as a finding about the file with nothing in
        # it (#263-shaped: a "finding" that names nothing is indistinguishable
        # from a checker that never ran).
        #
        # #752's sweep recorded this adapter as the model and noted a residual:
        # only the *empty*-stderr case reached `adapter`, while non-empty but
        # unlocated output still became `code: "syntax"`. That residual is live
        # on the first thing anyone tries —
        #
        #   ruby -c /nope/x.rb  ->  ruby: No such file or directory -- ... (LoadError)
        #   ruby -c .           ->  ruby: Is a directory -- . (LoadError)
        #
        # — so both cases now route through the same helper as its siblings (#753).
        errors = [{"line": None, "col": None, "severity": "error",
                   "code": "adapter",
                   "msg": tool_fault("ruby -c", result.returncode, output)}]

    emit({"tool": "ruby-check", "file": file, "ok": False, "count": len(errors),
          "errors": errors, "duration_ms": duration})


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # never leave stdout empty — callers json.loads() it
        file = sys.argv[1] if len(sys.argv) > 1 else ""
        emit({"tool": "ruby-check", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": str(exc)[:300]}],
              # Empty only if `main` raised before it set the clock -- an argv
              # it could not read, an import that failed. Nothing had begun
              # then, so `0` is the elapsed rather than a stand-in for one.
              "duration_ms": (int((time.time() - _STARTED[0]) * 1000)
                              if _STARTED else 0)})
