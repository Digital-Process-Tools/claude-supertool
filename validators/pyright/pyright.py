#!/usr/bin/env python3
"""pyright validator adapter — Python type-check via pyright --outputjson.

Requires `pyright` on PATH. Absent, this reports the third state — `skipped`
with the reason — rather than the `ok: true` it emitted until #1202, which was a
clean verdict about a file nothing type-checked. Name this validator in
`$SUPERTOOL_REQUIRE_VALIDATORS` to turn that absence into a loud error instead.

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
from source_context import context_fields
from refusal import absent, skipped

TOOL = "pyright"
INSTALL_HINT = ("pyright not found on PATH — this file was NOT type-checked "
                "(`npm install -g pyright`)")


def emit(d: dict) -> None:
    print(json.dumps(d))


def _skip(file: str, start: float, reason: str) -> None:
    emit(absent(TOOL, file, reason, int((time.time() - start) * 1000)))


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
        _skip(file, start, INSTALL_HINT)
        return

    try:
        result = subprocess.run(
            ["pyright", "--outputjson", file],
            capture_output=True,
            text=True,
            timeout=60, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        # `which` said yes and exec said no. Still an absent tool, so still the
        # third state — and still escalatable.
        _skip(file, start, "pyright on PATH but could not be executed")
        return
    except subprocess.TimeoutExpired:
        # Loud, not a skip: the binary exists and was invoked, so a hang is a
        # validator failure. But `adapter`, not a code of its own — that word is
        # the only one the core routes to the third state (SCHEMA.md, "the
        # reserved code for 'no verdict was obtained'"). Spelled `timeout`, this
        # payload was one error *about the file*: cached until the file's hash
        # changed, subtracted from a baseline, and counted as a new finding by
        # the rollback path, which reverts the edit (#1464).
        emit({"tool": "pyright", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter",
                          "msg": "timeout — pyright did not return within 60s; "
                                 "the file was NOT type-checked"}],
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
        # Nothing on either stream. pyright always prints a report under
        # `--outputjson`, so this is a run that produced no information at
        # all — a killed process, a wrapper that swallowed both streams — and
        # `ok: true` over it is a clean verdict about a file nothing read
        # (#1601, the same class as the three adapters that issue names).
        # `skipped()` directly, not `_skip()`: that routes through
        # `refusal.absent()`, which is reserved for a tool that is not there
        # and whose `$SUPERTOOL_REQUIRE_VALIDATORS` message reads "named ...
        # but could not run". pyright was found and did run; it just said
        # nothing.
        emit(skipped(TOOL, file,
                     "pyright produced no output on either stream, so this "
                     "run says nothing about the file",
                     duration))
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
        err.update(context_fields(file, line))
        errors.append(err)

    ok = len(errors) == 0
    emit({"tool": "pyright", "file": file, "ok": ok, "count": len(errors),
          "errors": errors, "duration_ms": duration})


if __name__ == "__main__":
    main()
