#!/usr/bin/env python3
"""xmllint validator adapter — XML well-formedness via libxml2.

Stdlib only. Reference implementation per validators/SCHEMA.md.
Usage:  xmllint.py <file>
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
        emit({"tool": "xmllint", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return
    file = sys.argv[1]
    start = time.time()
    try:
        # #150 XXE defence-in-depth: `--nonet` blocks network access during
        # entity resolution (DTDs, external entities); `--noent` resolves
        # entities to their text rather than fetching them. Both narrow what
        # libxml2 will do with attacker-influenced XML during validation.
        r = subprocess.run(
            ["xmllint", "--noout", "--nonet", "--noent", file],
            capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
        )
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
            ln = int(m.group(1))
            err = {"line": ln, "col": None, "severity": "error", "code": "xml",
                   "msg": m.group(2).strip()[:200]}
            err["source_context"] = source_context(file, ln)
            errors.append(err)
    if not errors:
        errors = [{"line": None, "col": None, "severity": "error", "code": "xml",
                   "msg": (out or "unknown error")[:300]}]
    emit({"tool": "xmllint", "file": file, "ok": False, "count": len(errors),
          "errors": errors, "duration_ms": dur})


if __name__ == "__main__":
    main()
