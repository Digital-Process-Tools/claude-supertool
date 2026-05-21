#!/usr/bin/env python3
"""inilint validator adapter — INI syntax check via stdlib configparser.

Stdlib only. Reference implementation per validators/SCHEMA.md.
Usage:  inilint.py <file>
"""

from __future__ import annotations

import configparser
import json
import sys
import time


def emit(d: dict) -> None:
    print(json.dumps(d))


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": "inilint", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return

    file = sys.argv[1]
    start = time.time()

    parser = configparser.RawConfigParser()
    try:
        with open(file, "r", encoding="utf-8") as fh:
            parser.read_file(fh)
    except configparser.MissingSectionHeaderError as e:
        emit({"tool": "inilint", "file": file, "ok": False, "count": 1,
              "errors": [{"line": e.lineno, "col": None, "severity": "error",
                          "code": "syntax", "msg": str(e).strip()[:300]}],
              "duration_ms": int((time.time() - start) * 1000)})
        return
    except configparser.ParsingError as e:
        errors = []
        for lineno, msg in e.errors:
            errors.append({"line": lineno, "col": None, "severity": "error",
                           "code": "syntax", "msg": msg.strip()[:300]})
        if not errors:
            errors = [{"line": None, "col": None, "severity": "error",
                       "code": "syntax", "msg": str(e).strip()[:300]}]
        emit({"tool": "inilint", "file": file, "ok": False, "count": len(errors),
              "errors": errors,
              "duration_ms": int((time.time() - start) * 1000)})
        return
    except FileNotFoundError:
        emit({"tool": "inilint", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "file not found"}],
              "duration_ms": int((time.time() - start) * 1000)})
        return

    emit({"tool": "inilint", "file": file, "ok": True, "count": 0,
          "errors": [], "duration_ms": int((time.time() - start) * 1000)})


if __name__ == "__main__":
    main()
