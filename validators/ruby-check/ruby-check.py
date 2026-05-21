#!/usr/bin/env python3
"""ruby-check validator adapter — Ruby syntax check via `ruby -c`.

Requires ruby on PATH. Ships on most Mac/Linux by default.
If missing, exits 0 with a stderr warning (graceful degrade).
Usage:  ruby-check.py <file>
"""

from __future__ import annotations

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
        emit({"tool": "ruby-check", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return

    file = sys.argv[1]
    start = time.time()

    if not shutil.which("ruby"):
        print("ruby-check: ruby not found on PATH, skipping", file=sys.stderr)
        emit({"tool": "ruby-check", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": int((time.time() - start) * 1000)})
        return

    try:
        result = subprocess.run(
            ["ruby", "-c", file],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        print("ruby-check: ruby not found on PATH, skipping", file=sys.stderr)
        emit({"tool": "ruby-check", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": int((time.time() - start) * 1000)})
        return

    duration = int((time.time() - start) * 1000)

    if result.returncode == 0:
        emit({"tool": "ruby-check", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": duration})
        return

    # Parse ruby -c stderr: "file:line: message"
    errors = []
    pattern = re.compile(r"^(?:.*?):(\d+):\s+(.+)$")
    output = result.stderr.strip()
    for line in output.splitlines():
        m = pattern.match(line)
        if m:
            lineno, msg = m.groups()
            # Skip the "1 error found" summary line
            if re.match(r"^\d+\s+error", msg):
                continue
            errors.append({
                "line": int(lineno),
                "col": None,
                "severity": "error",
                "code": "syntax",
                "msg": msg.strip()[:300],
            })

    if not errors and output:
        errors = [{"line": None, "col": None, "severity": "error",
                   "code": "syntax", "msg": output[:300]}]

    emit({"tool": "ruby-check", "file": file, "ok": False, "count": len(errors),
          "errors": errors, "duration_ms": duration})


if __name__ == "__main__":
    main()
