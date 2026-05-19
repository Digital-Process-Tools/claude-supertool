#!/usr/bin/env python3
"""tsc-check validator adapter — TypeScript syntax check via tsc --noEmit.

Requires tsc on PATH. If missing, exits 0 with a stderr warning (graceful degrade).
Usage:  tsc-check.py <file>
"""

import json
import re
import shutil
import subprocess
import sys
import time


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
        print("tsc-check: tsc not found on PATH, skipping", file=sys.stderr)
        emit({"tool": "tsc-check", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": int((time.time() - start) * 1000)})
        return

    try:
        result = subprocess.run(
            ["tsc", "--noEmit", "--skipLibCheck", file],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        print("tsc-check: tsc not found on PATH, skipping", file=sys.stderr)
        emit({"tool": "tsc-check", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": int((time.time() - start) * 1000)})
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
            errors.append({
                "line": int(lineno),
                "col": int(col),
                "severity": severity if severity in ("error", "warning") else "error",
                "code": code,
                "msg": msg.strip()[:300],
            })

    if not errors and output:
        errors = [{"line": None, "col": None, "severity": "error",
                   "code": "syntax", "msg": output[:300]}]

    emit({"tool": "tsc-check", "file": file, "ok": False, "count": len(errors),
          "errors": errors, "duration_ms": duration})


if __name__ == "__main__":
    main()
