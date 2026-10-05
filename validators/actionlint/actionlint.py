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
from refusal import absent, guard_main
from spawnable import argv0, spawnable
from linebreaks import split_lines
from path_anchor import (anchor as _anchor, safe_realpath as _safe_realpath,
                          anchor_miss_message as _anchor_miss_message)

TOOL = "actionlint"
INSTALL_HINT = ("actionlint not found on PATH — this workflow was NOT linted "
                "(`brew install actionlint`)")




TIMEOUT_S = 30















COUNT_CONTRACT = {"count_basis": "measured", "errors_truncated": False}



























def _line_re(file: str) -> re.Pattern[str]:
    try:
        reported = os.path.relpath(file)
    except ValueError:










        reported = file









    real = _safe_realpath(file)
    extra = []
    if real and real != file:
        try:
            extra.append(os.path.relpath(real))
        except ValueError:
            extra.append(real)
    return _anchor(reported, r":(\d+):(\d+):\s+(.+?)(?:\s+\[([\w-]+)\])?$",
                    extra_paths=extra)


def parse_diagnostics(output: str, file: str) -> list[dict]:





    line_re = _line_re(file)
    errors = []
    for line in split_lines(output):
        m = line_re.search(line)
        if m:
            lineno, col, msg, rule = m.groups()
            ln = int(lineno)
            err = {
                "line": ln,
                "col": int(col),
                "severity": "error",
                "code": rule or "syntax-check",
                "msg": msg.strip()[:300],
            }
            err.update(context_fields(file, ln))
            errors.append(err)
    return errors


def emit(d: dict) -> None:
    print(json.dumps(d))


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": TOOL, "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return

    file = sys.argv[1]
    start = time.time()

    if not spawnable("actionlint"):
        emit(absent(TOOL, file, INSTALL_HINT,
                    int((time.time() - start) * 1000)))
        return

    try:
        result = subprocess.run(
            [argv0("actionlint"), "-no-color", "-oneline", "--", file],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:



        emit(absent(TOOL, file, "actionlint on PATH but could not be executed "
                                "— this workflow was NOT linted",
                    int((time.time() - start) * 1000)))
        return
    except subprocess.TimeoutExpired:



        emit({"tool": TOOL, "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter",
                          "msg": f"timeout — actionlint did not return within {TIMEOUT_S}s; "
                                 "the file was NOT checked"}],
              "duration_ms": int((time.time() - start) * 1000)})
        return

    duration = int((time.time() - start) * 1000)

    if result.returncode == 0:
        emit({"tool": TOOL, "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": duration, **COUNT_CONTRACT})
        return

    output = (result.stdout + result.stderr).strip()
    errors = parse_diagnostics(output, file)

    if not errors and output:




        errors = [{"line": None, "col": None, "severity": "error",
                   "code": "lint",
                   "msg": _anchor_miss_message(file, output, output[:300])}]

    emit({"tool": TOOL, "file": file, "ok": False, "count": len(errors),
          "errors": errors, "duration_ms": duration, **COUNT_CONTRACT})


if __name__ == "__main__":
    guard_main(TOOL, main)
