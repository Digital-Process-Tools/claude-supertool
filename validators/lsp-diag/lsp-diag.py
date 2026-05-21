#!/usr/bin/env python3
"""LSP-diag validator adapter.

Calls `supertool diag:FILE` and converts the text output to a validators/SCHEMA.md
JSON receipt. Reuses the long-lived MCP daemon — sub-second when warm.

Why not call MCP directly: the supertool dispatch handles config lookup, daemon
auto-spawn, and tool routing. Reusing it keeps the validator dumb.

Usage:  lsp-diag.py <file>
Output: one JSON object on stdout.
Exit:   0 (always).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time


def emit(obj: dict) -> None:
    print(json.dumps(obj))


def parse_cclsp_diagnostics(text: str, file: str) -> list[dict]:
    """Parse cclsp's get_diagnostics text output into the SCHEMA error shape.

    cclsp shapes observed:
      "No diagnostics found for /path. The file has no errors, warnings, or hints."
      "Found N diagnostic(s) for /path:\\n• [severity] message at line X, col Y"
      "[error] message at line X:Y"   (some variants)
    Falls back to one generic error if format doesn't match.
    """
    if "No diagnostics found" in text or "no errors, warnings, or hints" in text.lower():
        return []

    errors: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # Pattern 1: "• [severity] message at line X, col Y" or "at X:Y"
        m = re.search(r"\[?\b(error|warning|info|hint)\b\]?\s*[:\s]*(.+?)(?:\s+at\s+line\s+(\d+)(?:[,\s]+(?:col(?:umn)?\s+)?(\d+))?|\s+(\d+):(\d+))\s*$",
                      line, flags=re.IGNORECASE)
        if m:
            severity = m.group(1).lower()
            msg = m.group(2).strip(" •-:")
            ln = int(m.group(3)) if m.group(3) else (int(m.group(5)) if m.group(5) else None)
            col = int(m.group(4)) if m.group(4) else (int(m.group(6)) if m.group(6) else None)
            errors.append({"line": ln, "col": col, "severity": severity,
                           "code": "lsp", "msg": msg})
            continue
        # Pattern 2: standalone "X:Y: severity: message"
        m = re.match(r"^(\d+):(\d+):\s*(\w+):\s*(.+)$", line)
        if m:
            errors.append({"line": int(m.group(1)), "col": int(m.group(2)),
                           "severity": m.group(3).lower(),
                           "code": "lsp", "msg": m.group(4).strip()})
    if not errors:
        # Unrecognized but non-empty → keep the text as a single advisory
        if not text.startswith("diag:"):  # ignore supertool's own error messages
            errors.append({"line": None, "col": None, "severity": "info",
                           "code": "lsp", "msg": text.strip()[:500]})
    return errors


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": "lsp-diag", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return

    file = sys.argv[1]
    start = time.time()

    # Find the supertool binary — env override > sibling of this script > PATH
    supertool_bin = os.environ.get("SUPERTOOL_BIN")
    if not supertool_bin:
        # Validators live at supertool_dir/validators/lsp-diag/lsp-diag.py
        # supertool.py is at supertool_dir/supertool.py
        guess = os.path.join(os.path.dirname(__file__), "..", "..", "supertool.py")
        if os.path.isfile(guess):
            supertool_bin = guess
        else:
            supertool_bin = "supertool"

    try:
        r = subprocess.run(
            [sys.executable, supertool_bin, f"diag:{file}"] if supertool_bin.endswith(".py")
            else [supertool_bin, f"diag:{file}"],
            capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError:
        emit({"tool": "lsp-diag", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "supertool binary not found"}],
              "duration_ms": int((time.time() - start) * 1000)})
        return
    except subprocess.TimeoutExpired:
        emit({"tool": "lsp-diag", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "timeout"}],
              "duration_ms": 30000})
        return

    # Strip supertool's "--- diag:FILE ---" header line
    text = r.stdout
    text = re.sub(r"^---\s*diag:[^\n]*---\s*\n", "", text, count=1)

    # Daemon not ready or LSP not configured → skip rather than fail
    if "no LSP configured" in text or "MCP server" in text and "unavailable" in text:
        emit({"tool": "lsp-diag", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": int((time.time() - start) * 1000),
              "skipped": "LSP not configured or daemon down"})
        return

    errors = parse_cclsp_diagnostics(text, file)
    # rollback_on_fail is false for this validator — even with errors we report ok=True
    # so the edit isn't rolled back. The framework sees count>0 as advisory only.
    emit({"tool": "lsp-diag", "file": file,
          "ok": all(e.get("severity") != "error" for e in errors),
          "count": len(errors), "errors": errors,
          "duration_ms": int((time.time() - start) * 1000)})


if __name__ == "__main__":
    main()
