#!/usr/bin/env python3
"""tsc-check validator adapter — TypeScript syntax check via tsc --noEmit.

Requires tsc on PATH. Absent, this reports the third state — `skipped` with the
reason — rather than the `ok: true` it emitted until #1202, which was a clean
verdict about a file nothing type-checked. Name this validator in
`$SUPERTOOL_REQUIRE_VALIDATORS` to turn that absence into a loud error instead.

Usage:  tsc-check.py <file>
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
from refusal import absent

TOOL = "tsc-check"
INSTALL_HINT = ("tsc not found on PATH — this file was NOT type-checked "
                "(`npm install -g typescript`)")


# Budget for the one tool spawn below. A module constant rather than a literal
# in the call so the decline can name it: a caller reading "timeout" cannot
# tell a hung compiler from a busy machine, and the number is the first thing
# they need to decide which (#658).
TIMEOUT_S = 30


def emit(d: dict) -> None:
    print(json.dumps(d))


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": "tsc-check", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return

    file = sys.argv[1]
    start = time.time()

    if not shutil.which("tsc"):
        emit(absent(TOOL, file, INSTALL_HINT,
                    int((time.time() - start) * 1000)))
        return

    try:
        result = subprocess.run(
            ["tsc", "--noEmit", "--skipLibCheck", file],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        # `which` said yes and exec said no — a PATH entry that vanished
        # between the two, or a name that resolves to something unrunnable.
        # Still an absent tool, so still the third state.
        emit(absent(TOOL, file, "tsc on PATH but could not be executed — "
                                "this file was NOT type-checked",
                    int((time.time() - start) * 1000)))
        return
    except subprocess.TimeoutExpired:
        # See hadolint.py for why this is a finding rather than a skip, and
        # why its absence was worse than a wrong verdict: an escaping
        # TimeoutExpired leaves stdout empty and the caller crashes on
        # json.loads with nothing naming the tool or the budget. `tsc` is the
        # likeliest of the three to reach its budget honestly — a cold
        # TypeScript compile on a cold runner is not fast — which makes
        # saying so, rather than dying, worth more here than anywhere.
        emit({"tool": "tsc-check", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter",
                          "msg": f"timeout — tsc did not return within {TIMEOUT_S}s; "
                                 "the file was NOT checked"}],
              "duration_ms": int((time.time() - start) * 1000)})
        return

    duration = int((time.time() - start) * 1000)

    if result.returncode == 0:
        emit({"tool": "tsc-check", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": duration})
        return

    # Parse tsc output: "file(line,col): error TSxxxx: message"
    errors = []
    pattern = re.compile(r"^(?:.*?)\((\d+),(\d+)\):\s+(\w+)\s+(TS\d+):\s+(.+)$")
    output = (result.stdout + result.stderr).strip()
    for line in output.splitlines():
        m = pattern.match(line)
        if m:
            lineno, col, severity, code, msg = m.groups()
            ln = int(lineno)
            err = {
                "line": ln,
                "col": int(col),
                "severity": severity if severity in ("error", "warning") else "error",
                "code": code,
                "msg": msg.strip()[:300],
            }
            err.update(context_fields(file, ln))
            errors.append(err)

    if not errors and output:
        errors = [{"line": None, "col": None, "severity": "error",
                   "code": "syntax", "msg": output[:300]}]

    emit({"tool": "tsc-check", "file": file, "ok": False, "count": len(errors),
          "errors": errors, "duration_ms": duration})


if __name__ == "__main__":
    main()
