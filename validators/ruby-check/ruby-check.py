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

TOOL = "ruby-check"
INSTALL_HINT = "ruby not found on PATH — this file was NOT syntax-checked"




TIMEOUT_S = 30































def _diagnostic_re(file: str) -> re.Pattern[str]:
    real = _safe_realpath(file)
    extra = [real] if real and real != file else []
    return _anchor(file, r":(\d+):\s+(.+)$", extra_paths=extra)


SUMMARY = re.compile(r"^\d+\s+error")


def parse_diagnostics(out: str, file: str) -> list[dict]:





    pattern = _diagnostic_re(file)
    errors = []
    for line in split_lines(out):
        m = pattern.search(line)
        if m:
            lineno, msg = m.groups()
            if SUMMARY.match(msg):
                continue
            ln = int(lineno)
            errors.append({"line": ln, "col": None, "severity": "error",
                           "code": "syntax", "msg": msg.strip()[:300],
                           **context_fields(file, ln)})
    return errors


def emit(d: dict) -> None:
    print(json.dumps(d))


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": "ruby-check", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return

    file = sys.argv[1]
    start = time.time()

    if not spawnable("ruby"):
        emit(absent(TOOL, file, INSTALL_HINT,
                    int((time.time() - start) * 1000)))
        return

    try:
        result = subprocess.run(
            [argv0("ruby"), "-c", "--", file],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:



        emit(absent(TOOL, file, "ruby on PATH but could not be executed — "
                                "this file was NOT syntax-checked",
                    int((time.time() - start) * 1000)))
        return
    except subprocess.TimeoutExpired:










        emit({"tool": TOOL, "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter",
                          "msg": f"timed out (ruby -c exceeded {TIMEOUT_S}s)"}],
              "duration_ms": int((time.time() - start) * 1000)})
        return

    duration = int((time.time() - start) * 1000)

    if result.returncode == 0:
        emit({"tool": "ruby-check", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": duration})
        return


    output = result.stderr.strip()
    errors = parse_diagnostics(output, file)

    if not errors:























        errors = [{"line": None, "col": None, "severity": "error",
                   "code": "adapter",
                   "msg": _anchor_miss_message(
                       file, output,
                       tool_fault("ruby -c", result.returncode, output))}]

    emit({"tool": "ruby-check", "file": file, "ok": False, "count": len(errors),
          "errors": errors, "duration_ms": duration})


if __name__ == "__main__":





    guard_main(TOOL, main)
