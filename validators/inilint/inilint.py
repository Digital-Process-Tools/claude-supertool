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
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "common"))
from source_context import source_context


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
        err = {"line": e.lineno, "col": None, "severity": "error",
               "code": "syntax", "msg": str(e).strip()[:300]}
        if e.lineno is not None:
            err["source_context"] = source_context(file, e.lineno)
        emit({"tool": "inilint", "file": file, "ok": False, "count": 1,
              "errors": [err],
              "duration_ms": int((time.time() - start) * 1000)})
        return
    except configparser.ParsingError as e:
        errors = []
        for lineno, msg in e.errors:
            err = {"line": lineno, "col": None, "severity": "error",
                   "code": "syntax", "msg": msg.strip()[:300]}
            if lineno is not None:
                err["source_context"] = source_context(file, lineno)
            errors.append(err)
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
