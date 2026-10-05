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

TOOL = "hadolint"


def contained_target(file: str) -> str:












    if not file or os.path.isabs(file) or not file.startswith("-"):
        return file
    return os.path.join(os.curdir, file)


INSTALL_HINT = ("hadolint not found on PATH — this Dockerfile was NOT linted "
                "(`brew install hadolint`)")






TIMEOUT_S = 30


































def _pattern(file: str) -> re.Pattern[str]:
    real = _safe_realpath(file)
    extra = [real] if real and real != file else []
    return _anchor(file, r":(\d+)\s+((?:DL|SC)\d+)\s+(\w+):\s+(.+)$",
                    extra_paths=extra)


def parse_diagnostics(output: str, file: str) -> list[dict]:





    pattern = _pattern(file)
    errors = []
    for line in split_lines(output):
        m = pattern.search(line)
        if m:
            lineno, code, severity, msg = m.groups()
            ln = int(lineno)
            err = {
                "line": ln,
                "col": None,
                "severity": severity if severity in ("error", "warning", "info", "style") else "error",
                "code": code,
                "msg": msg.strip()[:300],
            }
            err.update(context_fields(file, ln))
            errors.append(err)
    return errors


def emit(d: dict) -> None:
    print(json.dumps(d))


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": "hadolint", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return

    file = sys.argv[1]
    start = time.time()

    if not spawnable("hadolint"):
        emit(absent(TOOL, file, INSTALL_HINT,
                    int((time.time() - start) * 1000)))
        return

    try:
        result = subprocess.run(
            [argv0("hadolint"), "--format", "tty", contained_target(file)],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:



        emit(absent(TOOL, file, "hadolint on PATH but could not be executed — "
                                "this Dockerfile was NOT linted",
                    int((time.time() - start) * 1000)))
        return
    except subprocess.TimeoutExpired:









        emit({"tool": "hadolint", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter",
                          "msg": f"timeout — hadolint did not return within {TIMEOUT_S}s; "
                                 "the file was NOT checked"}],
              "duration_ms": int((time.time() - start) * 1000)})
        return

    duration = int((time.time() - start) * 1000)

    if result.returncode == 0:
        emit({"tool": "hadolint", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": duration})
        return

    output = (result.stdout + result.stderr).strip()
    errors = parse_diagnostics(output, file)

    if not errors and output:




        errors = [{"line": None, "col": None, "severity": "error",
                   "code": "lint",
                   "msg": _anchor_miss_message(file, output, output[:300])}]

    emit({"tool": "hadolint", "file": file, "ok": False, "count": len(errors),
          "errors": errors, "duration_ms": duration})


if __name__ == "__main__":
    guard_main(TOOL, main)
