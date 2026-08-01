#!/usr/bin/env python3
"""terraform-check validator adapter — Terraform formatting check via `terraform fmt -check`.

Requires terraform CLI. If missing, exits 0 with a stderr warning.
Usage:  terraform-check.py <file>
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "common"))
from source_context import source_context
from refusal import tool_fault

# `terraform fmt -check -diff` uses *distinct exit codes*, which the adapter
# never looked at — every non-zero exit became `code: "formatting"` with the
# message "file needs terraform fmt formatting", a specific claim and a false
# one in three of the four shapes below (#753):
#
#   clean         rc=0  (nothing)
#   unformatted   rc=3  stdout: subject.tf
#                       stdout: --- old/subject.tf / +++ new/subject.tf / @@ ...
#   HCL syntax    rc=2  stderr: | Error: Argument or block definition required
#                       stderr: |   on subject.tf line 1, in resource "x":
#   missing path  rc=2  stderr: | Error: Invalid file or directory path
#   unreadable    rc=2  stderr: | Error: Failed to open file
#   bad flag      rc=2  stderr: | Error: Failed to parse command-line flags
#
# Two markers, because there are two different findings here. A **diff body on
# stdout** is the fmt verdict. An `on <file> line N` inside an Error block is a
# genuine HCL finding — but a *syntax* one, and telling its author to run
# `terraform fmt` is advice that cannot work: fmt is what could not parse it.
# Anything else on a non-zero exit is terraform failing, not a verdict.
#
# The diagnostics arrive wrapped in a box-drawing gutter and ANSI colour, so
# both the matching and the message strip those first.
ANSI = re.compile(r"\x1b\[[0-9;]*m")
GUTTER = "│╷╵"
LOCATED = re.compile(r"\bon\s+\S.*?\s+line\s+(\d+)\b")


def plain(text: str) -> str:
    """terraform's rendered diagnostic as one readable line."""
    stripped = ANSI.sub("", text or "")
    lines = [ln.lstrip(GUTTER).strip() for ln in stripped.splitlines()]
    return " ".join(" ".join(lines).split())


def is_fmt_verdict(stdout: str, file: str) -> bool:
    """Did `fmt -check` name this file as needing formatting?

    Keyed on the diff body or on the path terraform echoes back, not on "stdout
    is non-empty" — the latter would read any stray chatter on a failing exit as
    a formatting verdict, which is the mistake being fixed rather than a
    narrower version of it.
    """
    body = (stdout or "").strip()
    if not body:
        return False
    if "--- old/" in body or "+++ new/" in body:
        return True
    first = body.splitlines()[0].strip()
    return first == file or os.path.basename(first) == os.path.basename(file)


def diagnostic_line(body: str) -> int | None:
    """The source line terraform attributed to a diagnostic, or None.

    `on <file> line N, in <block>:` is the only thing in an Error block that
    places it in the file; without one, terraform is talking about itself.
    """
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

    if not shutil.which("terraform"):
        print("terraform-check: terraform not found on PATH, skipping", file=sys.stderr)
        emit({"tool": "terraform-check", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": int((time.time() - start) * 1000)})
        return

    try:
        r = subprocess.run(["terraform", "fmt", "-check", "-diff", file],
                           capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        print("terraform-check: terraform not found on PATH, skipping", file=sys.stderr)
        emit({"tool": "terraform-check", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": int((time.time() - start) * 1000)})
        return
    except subprocess.TimeoutExpired:
        emit({"tool": "terraform-check", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "timeout"}],
              "duration_ms": 30000})
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
                       "source_context": source_context(file, ln)}
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
    main()
