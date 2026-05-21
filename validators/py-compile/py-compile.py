#!/usr/bin/env python3
"""py-compile validator adapter — Python syntax check via py_compile.

Stdlib only. Reference implementation per validators/SCHEMA.md.
Usage:  py-compile.py <file>
"""

from __future__ import annotations

import json
import py_compile
import sys
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "common"))
from source_context import source_context


def emit(d: dict) -> None:
    print(json.dumps(d))


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": "py-compile", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return
    file = sys.argv[1]
    start = time.time()
    try:
        py_compile.compile(file, doraise=True)
    except py_compile.PyCompileError as e:
        # exc_value is the underlying SyntaxError
        sx = e.exc_value
        line = getattr(sx, "lineno", None)
        col = getattr(sx, "offset", None)
        msg = (getattr(sx, "msg", str(e)) or "").strip()[:300]
        err = {"line": line, "col": col, "severity": "error", "code": "syntax", "msg": msg}
        if line is not None:
            err["source_context"] = source_context(file, line)
        emit({"tool": "py-compile", "file": file, "ok": False, "count": 1,
              "errors": [err],
              "duration_ms": int((time.time() - start) * 1000)})
        return
    except FileNotFoundError:
        emit({"tool": "py-compile", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "file not found"}],
              "duration_ms": int((time.time() - start) * 1000)})
        return
    emit({"tool": "py-compile", "file": file, "ok": True, "count": 0,
          "errors": [], "duration_ms": int((time.time() - start) * 1000)})


if __name__ == "__main__":
    main()
