#!/usr/bin/env python3
"""node --check validator adapter — JS/TS syntax check.

Stdlib only. Reference implementation per validators/SCHEMA.md.
Usage:  node-check.py <file>
"""

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
from refusal import tool_fault

# `node --check` opens a syntax report with the resolved path of the file it
# read, alone on a line, then the offending source, a caret, and the banner:
#
#   /abs/path/subject.js:2
#   const b = (;
#              ^
#   SyntaxError: Unexpected token ';'
#       at wrapSafe (node:internal/modules/cjs/loader:1637:18)
#
# When node never reaches the file the shape is entirely different, and it is
# what the old `re.search(r":(\d+)(?::(\d+))?\b", out)` mined for a line number:
#
#   node:internal/modules/cjs/loader:1386      <- FIRST digit-colon in the output
#     throw err;
#   Error: Cannot find module '/nope/missing.js'
#       at node:internal/modules/cjs/loader:1383:15
#
# so a missing file was published as `code: "syntax"` at **line 1386** with
# `source_context` rendered for it (#753). Two exclusions keep the location
# honest: node's internal frames use the `node:` module scheme, and stack
# frames are indented under `    at `. Neither can be a path in the file.
LOCATION = re.compile(r"^(?!\s)(?!node:)(.+?):(\d+)$", re.MULTILINE)
BANNER = re.compile(r"\bSyntaxError\b")


def diagnostic_line(out: str, file: str) -> int | None:
    """The line node attributed to a diagnostic in *this* file, or None.

    Matched against the target rather than accepted from anywhere in the
    output. node prints the path it resolved, so the comparison is exact and
    an unrelated `path:digits` cannot donate its line.
    """
    target = os.path.normcase(os.path.realpath(file))
    for m in LOCATION.finditer(out):
        if os.path.normcase(os.path.realpath(m.group(1))) == target:
            return int(m.group(2))
    return None


def spoke_about_file(out: str, line: int | None) -> bool:
    """Did node produce a verdict about the file, or just fall over?

    **Ambiguity falls towards the file** (the rule settled in #745): a
    `SyntaxError` banner with no location node's report shape lets us pin is
    still a finding, because a node whose output shape was not anticipated must
    not have its findings relabelled out of a caller's error list. What makes
    that direction cheap is that neither branch drops anything — both emit one
    error with `ok: False`, and an `adapter` message names the exit code and the
    raw output, so a fault misread as a finding stays fully legible.

    An unlocated finding reports `line: None`. Reclassifying is one fix and
    inventing a line is another; a finding we cannot place is better placed
    nowhere than at a line borrowed from node's own internals.
    """
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
        r = subprocess.run(["node", "--check", file],
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
              "duration_ms": 30000})
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
        err = {"line": line, "col": None, "severity": "error",
               "code": "syntax", "msg": msg[:300]}
        if line is not None:
            err.update(context_fields(file, line))
    else:
        err = {"line": None, "col": None, "severity": "error", "code": "adapter",
               "msg": tool_fault("node --check", r.returncode, out)}
    emit({"tool": "node-check", "file": file, "ok": False, "count": 1,
          "errors": [err],
          "duration_ms": dur})


if __name__ == "__main__":
    main()
