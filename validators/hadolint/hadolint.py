#!/usr/bin/env python3
"""hadolint validator adapter — Dockerfile lint via hadolint CLI.

Requires hadolint on PATH. If missing, exits 0 with a stderr warning (graceful degrade).
Usage:  hadolint.py <file>
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
        emit({"tool": "hadolint", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return

    file = sys.argv[1]
    start = time.time()

    if not shutil.which("hadolint"):
        print("hadolint: hadolint not found on PATH, skipping", file=sys.stderr)
        emit({"tool": "hadolint", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": int((time.time() - start) * 1000)})
        return

    try:
        result = subprocess.run(
            ["hadolint", "--format", "tty", file],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        print("hadolint: hadolint not found on PATH, skipping", file=sys.stderr)
        emit({"tool": "hadolint", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": int((time.time() - start) * 1000)})
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
            errors.append({
                "line": int(lineno),
                "col": None,
                "severity": severity if severity in ("error", "warning", "info", "style") else "error",
                "code": code,
                "msg": msg.strip()[:300],
            })

    if not errors and output:
        errors = [{"line": None, "col": None, "severity": "error",
                   "code": "lint", "msg": output[:300]}]

    emit({"tool": "hadolint", "file": file, "ok": False, "count": len(errors),
          "errors": errors, "duration_ms": duration})


if __name__ == "__main__":
    main()
