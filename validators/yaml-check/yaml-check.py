#!/usr/bin/env python3
"""yaml-check validator adapter — YAML syntax check via PyYAML yaml.safe_load().

Requires PyYAML (pip install pyyaml). If missing, exits 0 with a stderr warning.
Usage:  yaml-check.py <file>
"""

from __future__ import annotations

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
        emit({"tool": "yaml-check", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return

    file = sys.argv[1]
    start = time.time()

    try:
        import yaml
    except ImportError:
        print("yaml-check: PyYAML not installed, skipping", file=sys.stderr)
        emit({"tool": "yaml-check", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": int((time.time() - start) * 1000)})
        return

    try:
        with open(file, "r", encoding="utf-8") as fh:
            yaml.safe_load(fh)
    except yaml.YAMLError as e:
        line = None
        col = None
        if hasattr(e, "problem_mark") and e.problem_mark is not None:
            line = e.problem_mark.line + 1
            col = e.problem_mark.column + 1
        msg = str(e).strip()[:300]
        err = {"line": line, "col": col, "severity": "error", "code": "syntax", "msg": msg}
        if line is not None:
            err["source_context"] = source_context(file, line)
        emit({"tool": "yaml-check", "file": file, "ok": False, "count": 1,
              "errors": [err],
              "duration_ms": int((time.time() - start) * 1000)})
        return
    except FileNotFoundError:
        emit({"tool": "yaml-check", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "file not found"}],
              "duration_ms": int((time.time() - start) * 1000)})
        return

    emit({"tool": "yaml-check", "file": file, "ok": True, "count": 0,
          "errors": [], "duration_ms": int((time.time() - start) * 1000)})


if __name__ == "__main__":
    main()
