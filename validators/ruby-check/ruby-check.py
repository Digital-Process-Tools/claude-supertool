#!/usr/bin/env python3
"""ruby-check validator adapter — Ruby syntax check via `ruby -c`.

Requires ruby on PATH. Ships on most Mac/Linux by default.
If missing, exits 0 with a stderr warning (graceful degrade).
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
from refusal import tool_fault


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
        print("ruby-check: ruby not found on PATH, skipping", file=sys.stderr)
        emit({"tool": "ruby-check", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": int((time.time() - start) * 1000)})
        return

    try:
        result = subprocess.run(
            ["ruby", "-c", file],
            capture_output=True,
            text=True,
            timeout=30, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        print("ruby-check: ruby not found on PATH, skipping", file=sys.stderr)
        emit({"tool": "ruby-check", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": int((time.time() - start) * 1000)})
        return

    duration = int((time.time() - start) * 1000)

    if result.returncode == 0:
        emit({"tool": "ruby-check", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": duration})
        return

    # Parse ruby -c stderr: "file:line: message"
    errors = []
    pattern = re.compile(r"^(?:.*?):(\d+):\s+(.+)$")
    output = result.stderr.strip()
    for line in output.splitlines():
        m = pattern.match(line)
        if m:
            lineno, msg = m.groups()
            # Skip the "1 error found" summary line
            if re.match(r"^\d+\s+error", msg):
                continue
            ln = int(lineno)
            err = {
                "line": ln,
                "col": None,
                "severity": "error",
                "code": "syntax",
                "msg": msg.strip()[:300],
            }
            err["source_context"] = source_context(file, ln)
            errors.append(err)

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
