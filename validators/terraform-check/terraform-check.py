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
from linebreaks import split_lines
from refusal import absent, guard_main, tool_fault
from spawnable import argv0, spawnable

TOOL = "terraform-check"


def contained_target(file: str) -> str:












    if not file or os.path.isabs(file) or not file.startswith("-"):
        return file
    return os.path.join(os.curdir, file)


INSTALL_HINT = ("terraform not found on PATH — this file was NOT "
                "format-checked")























ANSI = re.compile(r"\x1b\[[0-9;]*m")
GUTTER = "│╷╵"
LOCATED = re.compile(r"\bon\s+\S.*?\s+line\s+(\d+)\b")


def plain(text: str) -> str:

    stripped = ANSI.sub("", text or "")
    lines = [ln.lstrip(GUTTER).strip() for ln in stripped.splitlines()]
    return " ".join(" ".join(lines).split())


def is_fmt_verdict(stdout: str, file: str) -> bool:







    body = (stdout or "").strip()
    if not body:
        return False
    if "--- old/" in body or "+++ new/" in body:
        return True
    first = split_lines(body)[0].strip()
    return first == file or os.path.basename(first) == os.path.basename(file)


def diagnostic_line(body: str) -> int | None:





    m = LOCATED.search(body)
    return int(m.group(1)) if m else None


def emit(d: dict) -> None:
    print(json.dumps(d))


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": "terraform-check", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return

    file = sys.argv[1]
    start = time.time()

    if not spawnable("terraform"):
        emit(absent(TOOL, file, INSTALL_HINT,
                    int((time.time() - start) * 1000)))
        return

    try:
        r = subprocess.run([argv0("terraform"), "fmt", "-check", "-diff", contained_target(file)],
                           capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace")
    except FileNotFoundError:



        emit(absent(TOOL, file, "terraform on PATH but could not be executed — "
                                "this file was NOT format-checked",
                    int((time.time() - start) * 1000)))
        return
    except subprocess.TimeoutExpired:
        emit({"tool": "terraform-check", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "timeout"}],
              "duration_ms": int((time.time() - start) * 1000)})
        return

    dur = int((time.time() - start) * 1000)

    if r.returncode != 0:
        if is_fmt_verdict(r.stdout, file):
            diff = (r.stdout or "").strip()[:500]
            err = {"line": None, "col": None, "severity": "error",
                   "code": "formatting",
                   "msg": "file needs terraform fmt formatting:\n" + diff}
        else:
            body = plain(r.stderr or r.stdout or "")
            ln = diagnostic_line(body)
            if ln is not None:
                err = {"line": ln, "col": None, "severity": "error",
                       "code": "syntax", "msg": body[:300],
                       **context_fields(file, ln)}
            else:
                err = {"line": None, "col": None, "severity": "error",
                       "code": "adapter",
                       "msg": tool_fault("terraform fmt -check", r.returncode, body)}
        emit({"tool": "terraform-check", "file": file, "ok": False, "count": 1,
              "errors": [err], "duration_ms": dur})
        return

    emit({"tool": "terraform-check", "file": file, "ok": True, "count": 0,
          "errors": [], "duration_ms": dur})


if __name__ == "__main__":
    guard_main(TOOL, main)
