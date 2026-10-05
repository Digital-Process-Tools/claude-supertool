#!/usr/bin/env python3






from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "common"))
from source_context import context_fields
from refusal import guard_main, tool_fault
from linebreaks import split_lines
from path_anchor import (anchor as _anchor, safe_realpath as _safe_realpath,
                          anchor_miss_message as _anchor_miss_message)



































def _diagnostic_re(file: str) -> re.Pattern[str]:
    real = _safe_realpath(file)
    extra = [real] if real and real != file else []
    return _anchor(file, r":(\d+):\s*(.+)", extra_paths=extra)


def parse_diagnostics(out: str, file: str) -> list[dict]:


    pattern = _diagnostic_re(file)
    errors = []
    for line in split_lines(out):
        m = pattern.search(line)
        if m:
            ln = int(m.group(1))
            errors.append({"line": ln, "col": None, "severity": "error",
                           "code": "xml", "msg": m.group(2).strip()[:200],
                           **context_fields(file, ln)})
    return errors


def emit(d: dict) -> None:
    print(json.dumps(d))


def contained_target(file: str) -> str:

    if not file or os.path.isabs(file) or not file.startswith("-"):
        return file
    return os.path.join(os.curdir, file)


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














        r = subprocess.run(
            ["xmllint", "--noout", "--nonet", "--noent", contained_target(file)],
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
              "duration_ms": int((time.time() - start) * 1000)})
        return
    dur = int((time.time() - start) * 1000)
    if r.returncode == 0:
        emit({"tool": "xmllint", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": dur})
        return

    out = r.stderr or ""
    errors = parse_diagnostics(out, file)
    if not errors:




        errors = [{"line": None, "col": None, "severity": "error",
                   "code": "adapter",
                   "msg": _anchor_miss_message(
                       file, out, tool_fault("xmllint", r.returncode, out))}]
    emit({"tool": "xmllint", "file": file, "ok": False, "count": len(errors),
          "errors": errors, "duration_ms": dur})


if __name__ == "__main__":
    guard_main("xmllint", main)
