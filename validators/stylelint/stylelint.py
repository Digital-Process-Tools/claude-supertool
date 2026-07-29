#!/usr/bin/env python3
"""stylelint validator adapter — CSS/SCSS lint via stylelint.

Stdlib only. Reference implementation per validators/SCHEMA.md.
Uses project-local stylelint config (auto-discovered by stylelint).
Usage:  stylelint.py <file>
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "common"))
from source_context import source_context


def emit(d: dict) -> None:
    print(json.dumps(d))


def _resolve_cmd() -> list:
    """Return argv prefix for stylelint. Tries global, falls back to npx."""
    if shutil.which("stylelint"):
        return ["stylelint"]
    if shutil.which("npx"):
        return ["npx", "--no-install", "stylelint"]
    return []


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": "stylelint", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return
    file = sys.argv[1]
    start = time.time()
    base = _resolve_cmd()
    if not base:
        emit({"tool": "stylelint", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "stylelint not found (neither global nor via npx)"}],
              "duration_ms": int((time.time() - start) * 1000)})
        return
    try:
        r = subprocess.run(base + ["--formatter", "json", file],
                           capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        emit({"tool": "stylelint", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "stylelint binary not found"}],
              "duration_ms": int((time.time() - start) * 1000)})
        return
    except subprocess.TimeoutExpired:
        emit({"tool": "stylelint", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "timeout"}],
              "duration_ms": 60000})
        return
    dur = int((time.time() - start) * 1000)
    out = (r.stdout or "").strip()
    if not out:
        emit({"tool": "stylelint", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": dur})
        return
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        emit({"tool": "stylelint", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "stylelint non-json output"}],
              "duration_ms": dur})
        return
    errors = []
    for item in data:
        for w in item.get("warnings", []):
            ln = w.get("line")
            err = {
                "line": ln,
                "col": w.get("column"),
                "severity": w.get("severity", "warning"),
                "code": w.get("rule"),
                "msg": (w.get("text") or "")[:300],
            }
            if ln is not None:
                err["source_context"] = source_context(file, ln)
            errors.append(err)
    emit({"tool": "stylelint", "file": file, "ok": len(errors) == 0,
          "count": len(errors), "errors": errors, "duration_ms": dur})


if __name__ == "__main__":
    main()
