#!/usr/bin/env python3
"""gofmt-check validator adapter — Go formatting check via `gofmt -l`.

Requires gofmt (ships with Go). Absent, this reports the third state — `skipped`
with the reason — rather than the `ok: true` it emitted until #1202, which was a
clean verdict about a file nothing formatted-checked. Name this validator in
`$SUPERTOOL_REQUIRE_VALIDATORS` to turn that absence into a loud error instead.

Usage:  gofmt-check.py <file>
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
from source_context import context_fields
from refusal import absent, guard_main, tool_fault
from linebreaks import split_lines
from path_anchor import anchor as _anchor, safe_realpath as _safe_realpath

TOOL = "gofmt-check"
INSTALL_HINT = ("gofmt not found on PATH — this file was NOT format-checked "
                "(install Go)")

# gofmt splits its two answers across the two streams and two exit codes, and
# the adapter read neither until #753:
#
#   clean         rc=0  (nothing)
#   unformatted   rc=0  stdout: subject.go        <- the `-l` signal
#   malformed     rc=2  stderr: subject.go:3:12: expected ')', found '{'
#   unopenable    rc=2  stderr: stat subject.go: no such file or directory
#
# `if r.returncode != 0` published the last two identically as `code: "syntax"`
# with the raw stderr, so a path gofmt could not stat became a Go syntax error
# — and a genuine parse error threw away the `line:col` gofmt had just handed
# over, reporting `line: None` and no source context.
#
# The marker is a located `path:line:col: message`. `stat ...: no such file`
# carries no line, which is precisely what distinguishes it.
#
# Anchored on the invoked path itself (#1934) rather than a bare `.+?`: the
# non-greedy wildcard used to discard the path instead of matching it, so it
# bound to the *earliest* `:digit:digit:` anywhere in the line — including
# one supplied by a filename crafted to contain its own `N:M: ` sequence.
# Building the pattern from `file` means only a spelling of the path gofmt
# was actually invoked against can start a match (see `path_anchor.py`,
# #1937, for what "a spelling of" widened to after this comment was first
# written).
#
# Tolerant of the spellings a real gofmt can echo that back in (#1937) --
# and of gofmt (or something upstream) reporting a symlinked invoked path's
# RESOLVED form instead, via `extra_paths=[realpath]` below -- see
# validators/common/path_anchor.py for both widenings. The `path` capture
# group this used to carry is dropped: nothing downstream ever read
# `m.group("path")`, only "line", "col" and "msg".
def _diagnostic_re(file: str) -> re.Pattern[str]:
    real = _safe_realpath(file)
    extra = [real] if real and real != file else []
    return _anchor(file, r":(?P<line>\d+):(?P<col>\d+):\s*(?P<msg>.+)$",
                    extra_paths=extra)


def parse_diagnostics(out: str, file: str) -> list[dict]:
    """Every located gofmt diagnostic. Empty means gofmt never parsed the file
    — `stat ...: no such file or directory` lands here and carries no line.

    Extracted so the rule can be driven in process on every platform; the
    fake-binary fixture is POSIX-only (see tests/test_adapter_tool_vs_file_753.py).
    """
    pattern = _diagnostic_re(file)
    errors = []
    for raw in split_lines(out):
        m = pattern.match(raw)
        if m:
            ln = int(m.group("line"))
            errors.append({
                "line": ln, "col": int(m.group("col")), "severity": "error",
                "code": "syntax", "msg": m.group("msg").strip()[:300],
                **context_fields(file, ln),
            })
    return errors


def emit(d: dict) -> None:
    print(json.dumps(d))


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": "gofmt-check", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return

    file = sys.argv[1]
    start = time.time()

    if not shutil.which("gofmt"):
        emit(absent(TOOL, file, INSTALL_HINT,
                    int((time.time() - start) * 1000)))
        return

    try:
        r = subprocess.run(["gofmt", "-l", file],
                           capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        # `which` said yes and exec said no — a PATH entry that vanished
        # between the two, or a name that resolves to something unrunnable.
        # Still an absent tool, so still the third state.
        emit(absent(TOOL, file, "gofmt on PATH but could not be executed — "
                                "this file was NOT format-checked",
                    int((time.time() - start) * 1000)))
        return
    except subprocess.TimeoutExpired:
        emit({"tool": "gofmt-check", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "timeout"}],
              "duration_ms": int((time.time() - start) * 1000)})
        return

    dur = int((time.time() - start) * 1000)

    if r.returncode != 0:
        out = (r.stderr or "") + (r.stdout or "")
        errors = parse_diagnostics(out, file)
        if not errors:
            errors = [{"line": None, "col": None, "severity": "error",
                       "code": "adapter",
                       "msg": tool_fault("gofmt -l", r.returncode, out)}]
        emit({"tool": "gofmt-check", "file": file, "ok": False,
              "count": len(errors), "errors": errors, "duration_ms": dur})
        return

    # gofmt -l prints the filename if formatting is needed, empty if clean
    if r.stdout.strip():
        emit({"tool": "gofmt-check", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "formatting",
                          "msg": "file needs gofmt formatting (run: gofmt -w " + file + ")"}],
              "duration_ms": dur})
        return

    emit({"tool": "gofmt-check", "file": file, "ok": True, "count": 0,
          "errors": [], "duration_ms": dur})


if __name__ == "__main__":
    guard_main(TOOL, main)
