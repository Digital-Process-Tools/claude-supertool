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
from linebreaks import lf_line_of_v8_line






















LOCATION = re.compile(r"^(?!\s)(?!node:)(.+?):(\d+)$", re.MULTILINE)
BANNER = re.compile(r"\bSyntaxError\b")


def diagnostic_line(out: str, file: str) -> int | None:






    target = os.path.normcase(os.path.realpath(file))
    for m in LOCATION.finditer(out):
        if os.path.normcase(os.path.realpath(m.group(1))) == target:
            return int(m.group(2))
    return None


def file_line(file: str, node_line: int | None) -> int | None:


























    if node_line is None:
        return None
    try:
        text = pathlib.Path(file).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return node_line
    return lf_line_of_v8_line(text, node_line)


def spoke_about_file(out: str, line: int | None) -> bool:














    return line is not None or bool(BANNER.search(out))


def emit(d: dict) -> None:
    print(json.dumps(d))


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": "node-check", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return
    file = sys.argv[1]
    start = time.time()
    try:
        r = subprocess.run(["node", "--check", "--", file],
                           capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        emit({"tool": "node-check", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "node binary not found"}],
              "duration_ms": int((time.time() - start) * 1000)})
        return
    except subprocess.TimeoutExpired:
        emit({"tool": "node-check", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "timeout"}],
              "duration_ms": int((time.time() - start) * 1000)})
        return
    dur = int((time.time() - start) * 1000)
    if r.returncode == 0:
        emit({"tool": "node-check", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": dur})
        return
    out = (r.stderr or "") + (r.stdout or "")
    line = diagnostic_line(out, file)
    if spoke_about_file(out, line):
        msg_m = re.search(r"((?:Syntax)?Error: .+)", out)
        msg = msg_m.group(1) if msg_m else " ".join(out.split())[:200]




        placed = file_line(file, line)
        if line is not None and placed is None:


            msg = (msg[:150] + f" [node reported line {line}; this file has no "
                   "such line as its lines are counted here, so the location "
                   "is not published]")
        err = {"line": placed, "col": None, "severity": "error",
               "code": "syntax", "msg": msg[:300]}
        if placed is not None:
            err.update(context_fields(file, placed))
    else:
        err = {"line": None, "col": None, "severity": "error", "code": "adapter",
               "msg": tool_fault("node --check", r.returncode, out)}
    emit({"tool": "node-check", "file": file, "ok": False, "count": 1,
          "errors": [err],
          "duration_ms": dur})


if __name__ == "__main__":
    guard_main("node-check", main)
