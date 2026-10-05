#!/usr/bin/env python3










from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "common"))
from source_context import context_fields
from refusal import absent, guard_main, tool_fault
from spawnable import argv0, spawnable
from linebreaks import split_lines
from path_anchor import (anchor as _anchor, safe_realpath as _safe_realpath,
                          anchor_miss_message as _anchor_miss_message)

TOOL = "gofmt-check"
INSTALL_HINT = ("gofmt not found on PATH — this file was NOT format-checked "
                "(install Go)")




































def _diagnostic_re(file: str) -> re.Pattern[str]:
    real = _safe_realpath(file)
    extra = [real] if real and real != file else []
    return _anchor(file, r":(?P<line>\d+):(?P<col>\d+):\s*(?P<msg>.+)$",
                    extra_paths=extra)


def parse_diagnostics(out: str, file: str) -> list[dict]:






    pattern = _diagnostic_re(file)
    errors = []
    for raw in split_lines(out):
        m = pattern.search(raw)
        if m:
            ln = int(m.group("line"))
            errors.append({
                "line": ln, "col": int(m.group("col")), "severity": "error",
                "code": "syntax", "msg": m.group("msg").strip()[:300],
                **context_fields(file, ln),
            })
    return errors


def emit(d: dict) -> None:
    print(json.dumps(d))


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": "gofmt-check", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return

    file = sys.argv[1]
    start = time.time()

    if not spawnable("gofmt"):
        emit(absent(TOOL, file, INSTALL_HINT,
                    int((time.time() - start) * 1000)))
        return

    try:
        r = subprocess.run([argv0("gofmt"), "-l", "--", file],
                           capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace")
    except FileNotFoundError:



        emit(absent(TOOL, file, "gofmt on PATH but could not be executed — "
                                "this file was NOT format-checked",
                    int((time.time() - start) * 1000)))
        return
    except subprocess.TimeoutExpired:
        emit({"tool": "gofmt-check", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "timeout"}],
              "duration_ms": int((time.time() - start) * 1000)})
        return

    dur = int((time.time() - start) * 1000)

    if r.returncode != 0:
        out = (r.stderr or "") + (r.stdout or "")
        errors = parse_diagnostics(out, file)
        if not errors:





            errors = [{"line": None, "col": None, "severity": "error",
                       "code": "adapter",
                       "msg": _anchor_miss_message(
                           file, out, tool_fault("gofmt -l", r.returncode, out))}]
        emit({"tool": "gofmt-check", "file": file, "ok": False,
              "count": len(errors), "errors": errors, "duration_ms": dur})
        return


    if r.stdout.strip():
        emit({"tool": "gofmt-check", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "formatting",
                          "msg": "file needs gofmt formatting (run: gofmt -w " + file + ")"}],
              "duration_ms": dur})
        return

    emit({"tool": "gofmt-check", "file": file, "ok": True, "count": 0,
          "errors": [], "duration_ms": dur})


if __name__ == "__main__":
    guard_main(TOOL, main)
