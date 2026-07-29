#!/usr/bin/env python3
"""bash-check validator adapter — bash syntax check via `bash -n`.

Stdlib only. Reference implementation per validators/SCHEMA.md.
Usage:  bash-check.py <file>
"""

from __future__ import annotations

import json
import re
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
        emit({"tool": "bash-check", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return
    file = sys.argv[1]
    start = time.time()
    try:
        r = subprocess.run(["bash", "-n", file],
                           capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        emit({"tool": "bash-check", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "bash binary not found"}],
              "duration_ms": int((time.time() - start) * 1000)})
        return
    except subprocess.TimeoutExpired:
        emit({"tool": "bash-check", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "timeout"}],
              "duration_ms": 10000})
        return
    dur = int((time.time() - start) * 1000)
    if r.returncode == 0:
        emit({"tool": "bash-check", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": dur})
        return
    # stderr lines: "file: line N: msg" or "file: line N: syntax error near unexpected token ..."
    out = r.stderr or ""
    errors = []
    for line in out.splitlines():
        m = re.search(r":\s*line\s+(\d+):\s*(.+)", line)
        if m:
            ln = int(m.group(1))
            err = {"line": ln, "col": None, "severity": "error", "code": "syntax",
                   "msg": m.group(2).strip()[:200]}
            err["source_context"] = source_context(file, ln)
            errors.append(err)
    if not errors:
        errors = [{"line": None, "col": None, "severity": "error",
                   "code": "syntax", "msg": (out or "unknown error")[:300]}]
    emit({"tool": "bash-check", "file": file, "ok": False, "count": len(errors),
          "errors": errors, "duration_ms": dur})


if __name__ == "__main__":
    main()
