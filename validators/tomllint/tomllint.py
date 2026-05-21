#!/usr/bin/env python3
"""tomllint validator adapter — TOML syntax check via stdlib tomllib (3.11+) or tomli.

Stdlib only on Python 3.11+. Falls back to tomli third-party package. Graceful skip
if neither is available. Reference implementation per validators/SCHEMA.md.
Usage:  tomllint.py <file>
"""

from __future__ import annotations

import json
import sys
import time


def emit(d: dict) -> None:
    print(json.dumps(d))


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": "tomllint", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return

    file = sys.argv[1]
    start = time.time()

    # Resolve TOML parser: stdlib tomllib (3.11+) or third-party tomli
    tomllib = None
    if sys.version_info >= (3, 11):
        import tomllib as _tomllib
        tomllib = _tomllib
    else:
        try:
            import tomli as _tomli
            tomllib = _tomli
        except ImportError:
            pass

    if tomllib is None:
        print(
            "tomllint: tomllib not available (Python < 3.11 and tomli not installed), skipping",
            file=sys.stderr,
        )
        emit({"tool": "tomllint", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": int((time.time() - start) * 1000)})
        return

    try:
        with open(file, "rb") as fh:
            tomllib.load(fh)
    except tomllib.TOMLDecodeError as e:
        msg = str(e).strip()[:300]
        # tomllib embeds line info in the message; try to extract it
        line = None
        col = None
        import re
        m = re.search(r"line\s+(\d+)", msg, re.IGNORECASE)
        if m:
            line = int(m.group(1))
        m2 = re.search(r"col(?:umn)?\s+(\d+)", msg, re.IGNORECASE)
        if m2:
            col = int(m2.group(1))
        emit({"tool": "tomllint", "file": file, "ok": False, "count": 1,
              "errors": [{"line": line, "col": col, "severity": "error",
                          "code": "syntax", "msg": msg}],
              "duration_ms": int((time.time() - start) * 1000)})
        return
    except FileNotFoundError:
        emit({"tool": "tomllint", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "file not found"}],
              "duration_ms": int((time.time() - start) * 1000)})
        return

    emit({"tool": "tomllint", "file": file, "ok": True, "count": 0,
          "errors": [], "duration_ms": int((time.time() - start) * 1000)})


if __name__ == "__main__":
    main()
