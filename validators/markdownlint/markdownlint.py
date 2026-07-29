#!/usr/bin/env python3
"""markdownlint validator adapter — Markdown lint via markdownlint CLI.

Requires markdownlint on PATH. If missing, exits 0 with a stderr warning (graceful degrade).
Usage:  markdownlint.py <file>
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


def emit(d: dict) -> None:
    print(json.dumps(d))


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": "markdownlint", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return

    file = sys.argv[1]
    start = time.time()

    if not shutil.which("markdownlint"):
        print("markdownlint: markdownlint not found on PATH, skipping", file=sys.stderr)
        emit({"tool": "markdownlint", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": int((time.time() - start) * 1000)})
        return

    try:
        result = subprocess.run(
            ["markdownlint", file],
            capture_output=True,
            text=True,
            timeout=30, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        print("markdownlint: markdownlint not found on PATH, skipping", file=sys.stderr)
        emit({"tool": "markdownlint", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": int((time.time() - start) * 1000)})
        return

    duration = int((time.time() - start) * 1000)

    if result.returncode == 0:
        emit({"tool": "markdownlint", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": duration})
        return

    # Parse markdownlint output: "file:line:col rule/description"
    # or "file:line rule/description" (no col)
    errors = []
    pattern = re.compile(r"^(?:.*?):(\d+)(?::(\d+))?\s+(MD\d+[^\s]*)\s+(.+)$")
    output = (result.stdout + result.stderr).strip()
    for line in output.splitlines():
        m = pattern.match(line)
        if m:
            lineno, col, code, msg = m.groups()
            ln = int(lineno)
            err = {
                "line": ln,
                "col": int(col) if col else None,
                "severity": "error",
                "code": code,
                "msg": msg.strip()[:300],
            }
            err["source_context"] = source_context(file, ln)
            errors.append(err)

    if not errors and output:
        errors = [{"line": None, "col": None, "severity": "error",
                   "code": "lint", "msg": output[:300]}]

    emit({"tool": "markdownlint", "file": file, "ok": False, "count": len(errors),
          "errors": errors, "duration_ms": duration})


if __name__ == "__main__":
    main()
