#!/usr/bin/env python3
"""phplint validator adapter — reference implementation.

Runs `php -l` on a file and emits a JSON object per validators/SCHEMA.md.
Stdlib only. Portable.

Usage:  phplint.py <file>
Output: one JSON object on stdout.
Exit:   0 (always, except on missing python — handled by interpreter).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "common"))
from source_context import context_fields
from refusal import tool_fault
from linebreaks import split_lines

# `php -l` exits non-zero for two unrelated reasons, and until #745 this adapter
# published both as `code: "parse"` at whatever line number a bare
# `on line (\d+)` search happened to land on. A PHP that cannot load an
# extension, or cannot open the path at all (`Could not open input file`, exit
# 1), was therefore reported as a syntax error in a file it never read - located
# at line 0, because the startup warning's own `in Unknown on line 0` is printed
# before any parse error and won the search.
#
# The tool is credited with having spoken about the file when its output carries
# a recognisable lint diagnostic: the linter's own `Errors parsing <file>`
# verdict line, or a `Parse error:` / `Fatal error:` banner (`php -l` reports
# compile-time fatals - redeclarations, illegal inheritance - as the latter).
# Some SAPIs prefix these with `PHP `, hence a search rather than a match.
LINT_DIAGNOSTIC = re.compile(r"^\s*(?:PHP\s+)?(?:Errors parsing\b|(?:Parse|Fatal) error\s*:)",
                             re.MULTILINE | re.IGNORECASE)
DIAGNOSTIC_BANNER = re.compile(r"(?:Parse|Fatal) error\s*:", re.IGNORECASE)
# `in <file> on line N`, excluding PHP's `in Unknown on line 0` - the marker of a
# message about the interpreter's own startup rather than about any file.
LOCATED = re.compile(r"\bin\s+(?!Unknown\b)\S.*?\bon line (\d+)")


def diagnostic_line(out: str) -> int | None:
    """The line number PHP attributed to a diagnostic, or None.

    Anchored to the diagnostic itself rather than searched across the whole
    output, so a startup warning printed first cannot donate its line 0 to a
    genuine parse error further down. Anchored **within** the line too, after
    the banner: the same donation happens inside one line — PHP prints a
    startup warning and a parse error on one line often enough — and it was
    only ever prevented across lines (#1486).
    """
    for raw in split_lines(out):
        banner = DIAGNOSTIC_BANNER.search(raw)
        if banner:
            m = re.search(r"on line (\d+)", raw[banner.end():])
            if m:
                return int(m.group(1))
    m = LOCATED.search(out)
    return int(m.group(1)) if m else None


def spoke_about_file(out: str, line: int | None) -> bool:
    """Did php produce a verdict about the file, or just fall over?

    **Ambiguity falls towards the file.** A located `in <file> on line N` with
    no banner this function recognises is still counted as a finding, because a
    PHP whose message shape was not anticipated must not have its findings
    relabelled out of the reader's sight. The cost of the other direction is
    bounded and the cost of this one is not: an `adapter` message names the exit
    code and the raw output, so a fault misread as a parse error is still fully
    legible - whereas a real syntax error relabelled `adapter` invites the
    reader to go looking at their toolchain.

    Nothing is dropped either way. Both branches emit one error with
    `ok: False`, so a misclassification in either direction changes the label
    and the line, never whether the file failed.
    """
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
            ["php", "-l", file],
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
    main()
