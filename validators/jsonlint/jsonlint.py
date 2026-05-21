#!/usr/bin/env python3
"""jsonlint validator adapter — JSON syntax check via stdlib json.load().

Stdlib only. Reference implementation per validators/SCHEMA.md.
Usage:  jsonlint.py <file>
"""

from __future__ import annotations

import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "common"))
from source_context import source_context


def emit(d: dict) -> None:
    print(json.dumps(d))


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": "jsonlint", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return
    file = sys.argv[1]
    start = time.time()
    try:
        with open(file, "r", encoding="utf-8") as fh:
            json.load(fh)
    except json.JSONDecodeError as e:
        msg = str(e).strip()[:300]
        err = {"line": e.lineno, "col": e.colno, "severity": "error",
               "code": "syntax", "msg": msg}
        err["source_context"] = source_context(file, e.lineno)
        emit({"tool": "jsonlint", "file": file, "ok": False, "count": 1,
              "errors": [err],
              "duration_ms": int((time.time() - start) * 1000)})
        return
    except FileNotFoundError:
        emit({"tool": "jsonlint", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "file not found"}],
              "duration_ms": int((time.time() - start) * 1000)})
        return
    emit({"tool": "jsonlint", "file": file, "ok": True, "count": 0,
          "errors": [], "duration_ms": int((time.time() - start) * 1000)})


if __name__ == "__main__":
    main()
