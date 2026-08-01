#!/usr/bin/env python3
"""xmllint validator adapter — XML well-formedness via libxml2.

Stdlib only. Reference implementation per validators/SCHEMA.md.
Usage:  xmllint.py <file>
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "common"))
from source_context import source_context
from refusal import tool_fault

# libxml announces a finding about the document as `file:LINE: parser error : ...`.
# Every other way it exits non-zero says nothing about the document's XML:
#
#   warning: failed to load external entity "x.xml"   (missing or unreadable path)
#   I/O error : Permission denied
#   Unknown option --bogusflag  + ~70 lines of usage
#
# All three used to be published as one `code: "xml"` error at file level, so a
# path typo and a wrong flag were both reported as malformed XML (#753). A
# located diagnostic is the marker; anything else on a non-zero exit is a
# libxml failure, not a verdict about the file.


# `file:LINE: message`. Extracted so the rule can be driven in process on every
# platform — the fake-binary fixture that drives the whole adapter is POSIX-only
# (see tests/test_adapter_tool_vs_file_753.py).
DIAGNOSTIC = re.compile(r"^.+?:(\d+):\s*(.+)")


def parse_diagnostics(out: str, file: str) -> list[dict]:
    """Every located diagnostic in libxml's stderr. Empty means it did not
    speak about the document."""
    errors = []
    for line in out.splitlines():
        m = DIAGNOSTIC.match(line)
        if m:
            ln = int(m.group(1))
            errors.append({"line": ln, "col": None, "severity": "error",
                           "code": "xml", "msg": m.group(2).strip()[:200],
                           "source_context": source_context(file, ln)})
    return errors


def emit(d: dict) -> None:
    print(json.dumps(d))


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": "xmllint", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return
    file = sys.argv[1]
    start = time.time()
    try:
        # #150 XXE defence-in-depth: `--nonet` blocks network access during
        # entity resolution (DTDs, external entities); `--noent` resolves
        # entities to their text rather than fetching them. Both narrow what
        # libxml2 will do with attacker-influenced XML during validation.
        r = subprocess.run(
            ["xmllint", "--noout", "--nonet", "--noent", file],
            capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        emit({"tool": "xmllint", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "xmllint binary not found"}],
              "duration_ms": int((time.time() - start) * 1000)})
        return
    except subprocess.TimeoutExpired:
        emit({"tool": "xmllint", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "timeout"}],
              "duration_ms": 30000})
        return
    dur = int((time.time() - start) * 1000)
    if r.returncode == 0:
        emit({"tool": "xmllint", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": dur})
        return
    # xmllint stderr: "file:LINE: parser error : msg" + context lines
    out = r.stderr or ""
    errors = parse_diagnostics(out, file)
    if not errors:
        errors = [{"line": None, "col": None, "severity": "error",
                   "code": "adapter",
                   "msg": tool_fault("xmllint", r.returncode, out)}]
    emit({"tool": "xmllint", "file": file, "ok": False, "count": len(errors),
          "errors": errors, "duration_ms": dur})


if __name__ == "__main__":
    main()
