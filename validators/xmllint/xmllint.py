#!/usr/bin/env python3
"""xmllint validator adapter — XML well-formedness via libxml2.

Stdlib only. Reference implementation per validators/SCHEMA.md.
Usage:  xmllint.py <file>
"""

import json
import re
import subprocess
import sys
import time


def emit(d: dict) -> None:
    print(json.dumps(d))


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": "xmllint", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return
    file = sys.argv[1]
    start = time.time()
    try:
        r = subprocess.run(["xmllint", "--noout", file],
                           capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        emit({"tool": "xmllint", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "xmllint binary not found"}],
              "duration_ms": int((time.time() - start) * 1000)})
        return
    except subprocess.TimeoutExpired:
        emit({"tool": "xmllint", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "timeout"}],
              "duration_ms": 30000})
        return
    dur = int((time.time() - start) * 1000)
    if r.returncode == 0:
        emit({"tool": "xmllint", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": dur})
        return
    # xmllint stderr: "file:LINE: parser error : msg" + context lines
    out = r.stderr or ""
    errors = []
    for line in out.splitlines():
        m = re.match(r"^.+?:(\d+):\s*(.+)", line)
        if m:
            errors.append({"line": int(m.group(1)), "col": None,
                           "severity": "error", "code": "xml",
                           "msg": m.group(2).strip()[:200]})
    if not errors:
        errors = [{"line": None, "col": None, "severity": "error", "code": "xml",
                   "msg": (out or "unknown error")[:300]}]
    emit({"tool": "xmllint", "file": file, "ok": False, "count": len(errors),
          "errors": errors, "duration_ms": dur})


if __name__ == "__main__":
    main()
