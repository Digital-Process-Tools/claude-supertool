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














LINT_DIAGNOSTIC = re.compile(r"^\s*(?:PHP\s+)?(?:Errors parsing\b|(?:Parse|Fatal) error\s*:)",
                             re.MULTILINE | re.IGNORECASE)


def contained_target(file: str) -> str:





















    if not file or os.path.isabs(file) or not file.startswith("-"):
        return file
    return os.path.join(os.curdir, file)


DIAGNOSTIC_BANNER = re.compile(r"(?:Parse|Fatal) error\s*:", re.IGNORECASE)


LOCATED = re.compile(r"\bin\s+(?!Unknown\b)\S.*?\bon line (\d+)")


def diagnostic_line(out: str) -> int | None:









    for raw in split_lines(out):
        banner = DIAGNOSTIC_BANNER.search(raw)
        if banner:
            m = re.search(r"on line (\d+)", raw[banner.end():])
            if m:
                return int(m.group(1))
    m = LOCATED.search(out)
    return int(m.group(1)) if m else None


def spoke_about_file(out: str, line: int | None) -> bool:















    return line is not None or bool(LINT_DIAGNOSTIC.search(out))


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
            ["php", "-l", contained_target(file)],
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
            "duration_ms": int((time.time() - start) * 1000),
        })
        return

    dur = int((time.time() - start) * 1000)

    if r.returncode == 0:
        emit({"tool": "phplint", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": dur})
        return

    out = (r.stdout or "") + (r.stderr or "")
    line = diagnostic_line(out)

    if spoke_about_file(out, line):
        msg = " ".join(out.split())[:300]
        err = {"line": line, "col": None, "severity": "error",
               "code": "parse", "msg": msg}
        if line is not None:
            err.update(context_fields(file, line))
    else:
        err = {"line": None, "col": None, "severity": "error", "code": "adapter",
               "msg": tool_fault("php -l", r.returncode, out)}
    emit({
        "tool": "phplint", "file": file, "ok": False, "count": 1,
        "errors": [err],
        "duration_ms": dur,
    })


if __name__ == "__main__":
    guard_main("phplint", main)
