#!/usr/bin/env python3
"""ruby-check validator adapter — Ruby syntax check via `ruby -c`.

Requires ruby on PATH. Ships on most Mac/Linux by default. Absent, this reports
the third state — `skipped` with the reason — rather than the `ok: true` it
emitted until #1202, which was a clean verdict about a file nothing parsed. Name
this validator in `$SUPERTOOL_REQUIRE_VALIDATORS` to turn that absence into a
loud error instead.

Usage:  ruby-check.py <file>
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "common"))
from source_context import source_context
from refusal import absent, tool_fault

TOOL = "ruby-check"
INSTALL_HINT = "ruby not found on PATH — this file was NOT syntax-checked"


# `file:line: message`. Extracted so the rule can be driven in process on every
# platform — the fake-binary fixture is POSIX-only (see
# tests/test_adapter_tool_vs_file_753.py).
DIAGNOSTIC = re.compile(r"^(?:.*?):(\d+):\s+(.+)$")
SUMMARY = re.compile(r"^\d+\s+error")


def parse_diagnostics(out: str, file: str) -> list[dict]:
    """Every located diagnostic in ruby's stderr, minus its own count summary.

    Empty means ruby exited non-zero without placing anything in the file —
    `ruby: No such file or directory -- x.rb (LoadError)` is the common shape.
    """
    errors = []
    for line in out.splitlines():
        m = DIAGNOSTIC.match(line)
        if m:
            lineno, msg = m.groups()
            if SUMMARY.match(msg):
                continue
            ln = int(lineno)
            errors.append({"line": ln, "col": None, "severity": "error",
                           "code": "syntax", "msg": msg.strip()[:300],
                           "source_context": source_context(file, ln)})
    return errors


def emit(d: dict) -> None:
    print(json.dumps(d))


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": "ruby-check", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return

    file = sys.argv[1]
    start = time.time()

    if not shutil.which("ruby"):
        emit(absent(TOOL, file, INSTALL_HINT,
                    int((time.time() - start) * 1000)))
        return

    try:
        result = subprocess.run(
            ["ruby", "-c", file],
            capture_output=True,
            text=True,
            timeout=30, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        # `which` said yes and exec said no — a PATH entry that vanished
        # between the two, or a name that resolves to something unrunnable.
        # Still an absent tool, so still the third state.
        emit(absent(TOOL, file, "ruby on PATH but could not be executed — "
                                "this file was NOT syntax-checked",
                    int((time.time() - start) * 1000)))
        return

    duration = int((time.time() - start) * 1000)

    if result.returncode == 0:
        emit({"tool": "ruby-check", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": duration})
        return

    # Parse ruby -c stderr: "file:line: message"
    output = result.stderr.strip()
    errors = parse_diagnostics(output, file)

    if not errors:
        # `ruby -c` exited non-zero without a `file:line:` diagnostic. That is
        # not "no syntax errors" (returncode == 0 already handled that above) —
        # resolving on PATH is not proof the spawned process ran like ruby (a
        # shim, alias, or broken install can exit non-zero silently). An
        # unexplained exit must stay a named error rather than fold into
        # `count: 0`, which reads as a finding about the file with nothing in
        # it (#263-shaped: a "finding" that names nothing is indistinguishable
        # from a checker that never ran).
        #
        # #752's sweep recorded this adapter as the model and noted a residual:
        # only the *empty*-stderr case reached `adapter`, while non-empty but
        # unlocated output still became `code: "syntax"`. That residual is live
        # on the first thing anyone tries —
        #
        #   ruby -c /nope/x.rb  ->  ruby: No such file or directory -- ... (LoadError)
        #   ruby -c .           ->  ruby: Is a directory -- . (LoadError)
        #
        # — so both cases now route through the same helper as its siblings (#753).
        errors = [{"line": None, "col": None, "severity": "error",
                   "code": "adapter",
                   "msg": tool_fault("ruby -c", result.returncode, output)}]

    emit({"tool": "ruby-check", "file": file, "ok": False, "count": len(errors),
          "errors": errors, "duration_ms": duration})


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # never leave stdout empty — callers json.loads() it
        file = sys.argv[1] if len(sys.argv) > 1 else ""
        emit({"tool": "ruby-check", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": str(exc)[:300]}],
              "duration_ms": 0})
