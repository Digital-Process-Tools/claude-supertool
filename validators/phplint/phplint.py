#!/usr/bin/env python3
"""phplint validator adapter — reference implementation.

Runs `php -l` on a file and emits a JSON object per validators/SCHEMA.md.
Stdlib only. Portable.

Usage:  phplint.py <file>
Output: one JSON object on stdout.
Exit:   0 (always, except on missing python — handled by interpreter).
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


def emit(obj: dict) -> None:
    print(json.dumps(obj))


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({
            "tool": "phplint", "file": "", "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": "no file arg"}],
            "duration_ms": 0,
        })
        return

    file = sys.argv[1]
    start = time.time()
    try:
        r = subprocess.run(
            ["php", "-l", file],
            capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        emit({
            "tool": "phplint", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": "php binary not found"}],
            "duration_ms": int((time.time() - start) * 1000),
        })
        return
    except subprocess.TimeoutExpired:
        emit({
            "tool": "phplint", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": "timeout"}],
            "duration_ms": 30000,
        })
        return

    dur = int((time.time() - start) * 1000)

    if r.returncode == 0:
        emit({"tool": "phplint", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": dur})
        return

    out = (r.stdout or "") + (r.stderr or "")
    m = re.search(r"on line (\d+)", out)
    line = int(m.group(1)) if m else None
    msg = " ".join(out.split())[:300]

    err = {"line": line, "col": None, "severity": "error", "code": "parse", "msg": msg}
    if line is not None:
        err["source_context"] = source_context(file, line)
    emit({
        "tool": "phplint", "file": file, "ok": False, "count": 1,
        "errors": [err],
        "duration_ms": dur,
    })


if __name__ == "__main__":
    main()
