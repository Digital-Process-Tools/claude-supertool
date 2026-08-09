#!/usr/bin/env python3
"""hadolint validator adapter — Dockerfile lint via hadolint CLI.

Requires hadolint on PATH. Absent, this reports the third state — `skipped` with
the reason — rather than the `ok: true` it emitted until #1202, which was a clean
verdict about a Dockerfile nothing linted. Name this validator in
`$SUPERTOOL_REQUIRE_VALIDATORS` to turn that absence into a loud error instead.

Usage:  hadolint.py <file>
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
from source_context import source_context
from refusal import absent

TOOL = "hadolint"
INSTALL_HINT = ("hadolint not found on PATH — this Dockerfile was NOT linted "
                "(`brew install hadolint`)")


# Budget for the one tool spawn below. A module constant rather than a literal
# in the call so the decline can name it: a caller reading "timeout" cannot
# tell a hung linter from a busy machine, and the number is the first thing
# they need to decide which (#658).
TIMEOUT_S = 30


def emit(d: dict) -> None:
    print(json.dumps(d))


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": "hadolint", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return

    file = sys.argv[1]
    start = time.time()

    if not shutil.which("hadolint"):
        emit(absent(TOOL, file, INSTALL_HINT,
                    int((time.time() - start) * 1000)))
        return

    try:
        result = subprocess.run(
            ["hadolint", "--format", "tty", file],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        print("hadolint: hadolint not found on PATH, skipping", file=sys.stderr)
        emit({"tool": "hadolint", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": int((time.time() - start) * 1000)})
        return
    except subprocess.TimeoutExpired:
        # Not a finding about the file, and not silence either. Without this
        # the exception escapes an adapter with no top-level handler, the
        # process dies on a traceback with **empty stdout**, and every caller
        # json.loads() that — so a slow linter surfaces as a JSONDecodeError
        # naming neither the tool nor the timeout. Same three-state collapse
        # #650 fixed for git: "could not answer" wearing the clothes of an
        # answer. It stays ok=False rather than becoming a skip, because the
        # binary was found and started (docs/validators.md — a post-spawn
        # failure is a finding with an `adapter` code, never a decline).
        emit({"tool": "hadolint", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter",
                          "msg": f"timeout — hadolint did not return within {TIMEOUT_S}s; "
                                 "the file was NOT checked"}],
              "duration_ms": int((time.time() - start) * 1000)})
        return

    duration = int((time.time() - start) * 1000)

    if result.returncode == 0:
        emit({"tool": "hadolint", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": duration})
        return

    # Parse hadolint tty output: "file:line DL1234 severity: message"
    errors = []
    pattern = re.compile(r"^(?:.*?):(\d+)\s+((?:DL|SC)\d+)\s+(\w+):\s+(.+)$")
    output = (result.stdout + result.stderr).strip()
    for line in output.splitlines():
        m = pattern.match(line)
        if m:
            lineno, code, severity, msg = m.groups()
            ln = int(lineno)
            err = {
                "line": ln,
                "col": None,
                "severity": severity if severity in ("error", "warning", "info", "style") else "error",
                "code": code,
                "msg": msg.strip()[:300],
            }
            err["source_context"] = source_context(file, ln)
            errors.append(err)

    if not errors and output:
        errors = [{"line": None, "col": None, "severity": "error",
                   "code": "lint", "msg": output[:300]}]

    emit({"tool": "hadolint", "file": file, "ok": False, "count": len(errors),
          "errors": errors, "duration_ms": duration})


if __name__ == "__main__":
    main()
