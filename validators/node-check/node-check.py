#!/usr/bin/env python3
"""node --check validator adapter — JS/TS syntax check.

Stdlib only. Reference implementation per validators/SCHEMA.md.
Usage:  node-check.py <file>
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time


def emit(d: dict) -> None:
    print(json.dumps(d))


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": "node-check", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return
    file = sys.argv[1]
    start = time.time()
    try:
        r = subprocess.run(["node", "--check", file],
                           capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        emit({"tool": "node-check", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "node binary not found"}],
              "duration_ms": int((time.time() - start) * 1000)})
        return
    except subprocess.TimeoutExpired:
        emit({"tool": "node-check", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "timeout"}],
              "duration_ms": 30000})
        return
    dur = int((time.time() - start) * 1000)
    if r.returncode == 0:
        emit({"tool": "node-check", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": dur})
        return
    out = (r.stderr or "") + (r.stdout or "")
    # node prints "file:LINE\n...SyntaxError: msg" or "file:LINE:COL\n..."
    m = re.search(r":(\d+)(?::(\d+))?\b", out)
    line = int(m.group(1)) if m else None
    col = int(m.group(2)) if m and m.group(2) else None
    msg_m = re.search(r"((?:Syntax)?Error: .+)", out)
    msg = msg_m.group(1) if msg_m else " ".join(out.split())[:200]
    emit({"tool": "node-check", "file": file, "ok": False, "count": 1,
          "errors": [{"line": line, "col": col, "severity": "error",
                      "code": "syntax", "msg": msg[:300]}],
          "duration_ms": dur})


if __name__ == "__main__":
    main()
