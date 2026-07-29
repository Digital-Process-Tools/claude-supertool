#!/usr/bin/env python3
"""pyright validator adapter — Python type-check via pyright --outputjson.

Requires `pyright` on PATH. If missing, exits 0 with a stderr warning (graceful
degrade) so the validator pipeline keeps moving when the tool isn't installed.

Usage:  pyright.py <file>

Output shape matches the supertool validator SCHEMA:
  {tool, file, ok, count, errors[], duration_ms}

Note on scope: pyright wants a project root (it walks up for pyproject.toml /
pyrightconfig.json). We pass a single file and let pyright resolve the project
from the file's location — same UX as tsc-check.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "common"))
from source_context import source_context


def emit(d: dict) -> None:
    print(json.dumps(d))


def _skip(file: str, start: float, msg: str) -> None:
    print(f"pyright: {msg}", file=sys.stderr)
    emit({"tool": "pyright", "file": file, "ok": True, "count": 0,
          "errors": [], "duration_ms": int((time.time() - start) * 1000)})


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": "pyright", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return

    file = sys.argv[1]
    start = time.time()

    if not shutil.which("pyright"):
        _skip(file, start, "pyright not found on PATH, skipping")
        return

    try:
        result = subprocess.run(
            ["pyright", "--outputjson", file],
            capture_output=True,
            text=True,
            timeout=60, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        _skip(file, start, "pyright not found on PATH, skipping")
        return
    except subprocess.TimeoutExpired:
        emit({"tool": "pyright", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "timeout", "msg": "pyright timed out after 60s"}],
              "duration_ms": int((time.time() - start) * 1000)})
        return

    duration = int((time.time() - start) * 1000)

    # pyright prints JSON to stdout. Exit code is non-zero when errors are
    # present — we trust the JSON content, not the exit code.
    raw = result.stdout.strip()
    if not raw:
        # No JSON — likely a config/launch failure. Surface stderr as a single
        # adapter-level error so the user sees it instead of silent pass.
        stderr = (result.stderr or "").strip()
        if stderr:
            emit({"tool": "pyright", "file": file, "ok": False, "count": 1,
                  "errors": [{"line": None, "col": None, "severity": "error",
                              "code": "adapter", "msg": stderr[:300]}],
                  "duration_ms": duration})
            return
        emit({"tool": "pyright", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": duration})
        return

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        emit({"tool": "pyright", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": f"JSON decode: {e}"[:300]}],
              "duration_ms": duration})
        return

    # pyright JSON: {generalDiagnostics: [{file, severity, message, range, rule}]}
    # range = {start: {line, character}, end: {line, character}} — 0-indexed.
    errors = []
    for d in data.get("generalDiagnostics", []):
        sev = d.get("severity", "error")
        # Only surface errors + warnings. Skip 'information' / 'hint'.
        if sev not in ("error", "warning"):
            continue
        rng = d.get("range", {}).get("start", {})
        # pyright is 0-indexed; supertool/source_context is 1-indexed.
        line = int(rng.get("line", 0)) + 1
        col = int(rng.get("character", 0)) + 1
        msg = (d.get("message") or "").strip()
        # Collapse multi-line messages — they explode the JSON output otherwise.
        msg = " · ".join(msg.splitlines())[:300]
        err = {
            "line": line,
            "col": col,
            "severity": sev,
            "code": d.get("rule") or "pyright",
            "msg": msg,
        }
        err["source_context"] = source_context(file, line)
        errors.append(err)

    ok = len(errors) == 0
    emit({"tool": "pyright", "file": file, "ok": ok, "count": len(errors),
          "errors": errors, "duration_ms": duration})


if __name__ == "__main__":
    main()
