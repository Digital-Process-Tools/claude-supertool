#!/usr/bin/env python3
"""terraform-check validator adapter — Terraform formatting check via `terraform fmt -check`.

Requires terraform CLI. If missing, exits 0 with a stderr warning.
Usage:  terraform-check.py <file>
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
        emit({"tool": "terraform-check", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return

    file = sys.argv[1]
    start = time.time()

    if not shutil.which("terraform"):
        print("terraform-check: terraform not found on PATH, skipping", file=sys.stderr)
        emit({"tool": "terraform-check", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": int((time.time() - start) * 1000)})
        return

    try:
        r = subprocess.run(["terraform", "fmt", "-check", "-diff", file],
                           capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        print("terraform-check: terraform not found on PATH, skipping", file=sys.stderr)
        emit({"tool": "terraform-check", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": int((time.time() - start) * 1000)})
        return
    except subprocess.TimeoutExpired:
        emit({"tool": "terraform-check", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "timeout"}],
              "duration_ms": 30000})
        return

    dur = int((time.time() - start) * 1000)

    if r.returncode != 0:
        diff = (r.stdout or r.stderr or "needs formatting").strip()[:500]
        emit({"tool": "terraform-check", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "formatting",
                          "msg": "file needs terraform fmt formatting:\n" + diff}],
              "duration_ms": dur})
        return

    emit({"tool": "terraform-check", "file": file, "ok": True, "count": 0,
          "errors": [], "duration_ms": dur})


if __name__ == "__main__":
    main()
