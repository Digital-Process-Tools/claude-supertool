#!/usr/bin/env python3
"""gofmt-check validator adapter — Go formatting check via `gofmt -l`.

Requires gofmt (ships with Go). If missing, exits 0 with a stderr warning.
Usage:  gofmt-check.py <file>
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time


def emit(d: dict) -> None:
    print(json.dumps(d))


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": "gofmt-check", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return

    file = sys.argv[1]
    start = time.time()

    if not shutil.which("gofmt"):
        print("gofmt-check: gofmt not found on PATH, skipping", file=sys.stderr)
        emit({"tool": "gofmt-check", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": int((time.time() - start) * 1000)})
        return

    try:
        r = subprocess.run(["gofmt", "-l", file],
                           capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        print("gofmt-check: gofmt not found on PATH, skipping", file=sys.stderr)
        emit({"tool": "gofmt-check", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": int((time.time() - start) * 1000)})
        return
    except subprocess.TimeoutExpired:
        emit({"tool": "gofmt-check", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "timeout"}],
              "duration_ms": 30000})
        return

    dur = int((time.time() - start) * 1000)

    if r.returncode != 0:
        msg = (r.stderr or "gofmt error").strip()[:300]
        emit({"tool": "gofmt-check", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "syntax", "msg": msg}],
              "duration_ms": dur})
        return

    # gofmt -l prints the filename if formatting is needed, empty if clean
    if r.stdout.strip():
        emit({"tool": "gofmt-check", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "formatting",
                          "msg": "file needs gofmt formatting (run: gofmt -w " + file + ")"}],
              "duration_ms": dur})
        return

    emit({"tool": "gofmt-check", "file": file, "ok": True, "count": 0,
          "errors": [], "duration_ms": dur})


if __name__ == "__main__":
    main()
