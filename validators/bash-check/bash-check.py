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
from refusal import guard_main, tool_fault
from linebreaks import split_lines
from quote_balance import unbalanced_quote_open


















DIAGNOSTIC = re.compile(r":\s*line\s+(\d+):\s*(.+)")


def _read_for_guess(file: str) -> str:
















    try:
        return pathlib.Path(file).read_text(errors="replace", encoding="utf-8")
    except OSError:
        return ""


def parse_diagnostics(out: str, file: str) -> list[dict]:












    errors = []
    text = None
    for line in split_lines(out):
        m = DIAGNOSTIC.search(line)
        if m:
            ln = int(m.group(1))
            err = {"line": ln, "col": None, "severity": "error",
                   "code": "syntax", "msg": m.group(2).strip()[:200],
                   **context_fields(file, ln)}
            if text is None:
                text = _read_for_guess(file)
            if text:
                guess_line = unbalanced_quote_open(text, ln)
                if guess_line is not None and guess_line != ln:
                    err["quote_open_guess"] = {
                        "line": guess_line,
                        "note": ("best-effort: a quote opened here looks still "
                                 "unclosed by the reported line. Not itself a "
                                 "diagnostic -- `line` above is bash's own and "
                                 "exact, this is a guess (#1810)."),
                    }
            errors.append(err)
    return errors


def emit(d: dict) -> None:
    print(json.dumps(d))


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": "bash-check", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return
    file = sys.argv[1]
    start = time.time()
    try:
        r = subprocess.run(["bash", "-n", "--", file],
                           capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        emit({"tool": "bash-check", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "bash binary not found"}],
              "duration_ms": int((time.time() - start) * 1000)})
        return
    except subprocess.TimeoutExpired:
        emit({"tool": "bash-check", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "timeout"}],
              "duration_ms": int((time.time() - start) * 1000)})
        return
    dur = int((time.time() - start) * 1000)
    if r.returncode == 0:
        emit({"tool": "bash-check", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": dur})
        return

    out = r.stderr or ""
    errors = parse_diagnostics(out, file)
    if not errors:
        errors = [{"line": None, "col": None, "severity": "error",
                   "code": "adapter",
                   "msg": tool_fault("bash -n", r.returncode, out)}]
    emit({"tool": "bash-check", "file": file, "ok": False, "count": len(errors),
          "errors": errors, "duration_ms": dur})


if __name__ == "__main__":
    guard_main("bash-check", main)
