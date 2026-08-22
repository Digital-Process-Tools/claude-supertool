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
from source_context import context_fields
from refusal import absent, guard_main, tool_fault
from linebreaks import split_lines
from path_anchor import (anchor as _anchor, safe_realpath as _safe_realpath,
                          anchor_miss_message as _anchor_miss_message)

TOOL = "ruby-check"
INSTALL_HINT = "ruby not found on PATH — this file was NOT syntax-checked"

# Hoisted out of the `subprocess.run` call so the decline below can name the
# number, and so `tests/_adapter_budget.inner_budget` reads one value rather
# than two spellings of it.
TIMEOUT_S = 30

# `file:line: message`. Extracted so the rule can be driven in process on every
# platform — the fake-binary fixture is POSIX-only (see
# tests/test_adapter_tool_vs_file_753.py).
#
# Anchored on the invoked path itself (#1934) rather than a bare `.*?`: the
# non-greedy wildcard used to discard the path instead of matching it, so it
# bound to the *earliest* `:digit:` anywhere in the line — including one
# supplied by a filename crafted to contain its own `N: ` sequence. Building
# the pattern from `file` means only a spelling of the path ruby was
# actually invoked against can start a match (see `path_anchor.py`, #1937,
# for what "a spelling of" widened to after this comment was first
# written).
#
# Tolerant of the spellings real ruby can echo that back in (#1937): two
# tests spawning the real ruby binary went red on windows-latest CI with a
# plain re.escape(file) anchor, matching nothing. Reasoned, not directly
# observed against a Windows machine here -- Ruby on Windows is known to
# normalise path separators to `/` in some of its own output, which this
# widens the anchor to tolerate either way.
#
# The SAME two tests then went red identically on ubuntu-latest, which
# ruled out a Windows-only cause for them: also tolerant of ruby (or
# something upstream) reporting a symlinked invoked path's RESOLVED form
# instead, via `extra_paths=[realpath]` below, ungated (every platform).
# See validators/common/path_anchor.py for both widenings.
def _diagnostic_re(file: str) -> re.Pattern[str]:
    real = _safe_realpath(file)
    extra = [real] if real and real != file else []
    return _anchor(file, r":(\d+):\s+(.+)$", extra_paths=extra)


SUMMARY = re.compile(r"^\d+\s+error")


def parse_diagnostics(out: str, file: str) -> list[dict]:
    """Every located diagnostic in ruby's stderr, minus its own count summary.

    Empty means ruby exited non-zero without placing anything in the file —
    `ruby: No such file or directory -- x.rb (LoadError)` is the common shape.
    """
    pattern = _diagnostic_re(file)
    errors = []
    for line in split_lines(out):
        m = pattern.match(line)
        if m:
            lineno, msg = m.groups()
            if SUMMARY.match(msg):
                continue
            ln = int(lineno)
            errors.append({"line": ln, "col": None, "severity": "error",
                           "code": "syntax", "msg": msg.strip()[:300],
                           **context_fields(file, ln)})
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
            timeout=TIMEOUT_S, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        # `which` said yes and exec said no — a PATH entry that vanished
        # between the two, or a name that resolves to something unrunnable.
        # Still an absent tool, so still the third state.
        emit(absent(TOOL, file, "ruby on PATH but could not be executed — "
                                "this file was NOT syntax-checked",
                    int((time.time() - start) * 1000)))
        return
    except subprocess.TimeoutExpired:
        # Until #1604 this arm did not exist and the `TimeoutExpired` escaped
        # `main()` into the module-level `except Exception` below, which
        # hardcoded `duration_ms: 0` until #1683 gave it `_STARTED` to measure
        # against. The code was right and the number was a
        # lie, and the number is what the suite reads: a stall is only
        # distinguishable from an adapter with broken error routing by whether
        # the elapsed time reaches the budget, so `after 0ms` beside "timed out
        # after 30 seconds" made a real wall unclassifiable and published it as
        # a verdict about the file (tests/_adapter_verdict.py,
        # `stalled_at_its_own_wall`, fourth clause).
        emit({"tool": TOOL, "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter",
                          "msg": f"timed out (ruby -c exceeded {TIMEOUT_S}s)"}],
              "duration_ms": int((time.time() - start) * 1000)})
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
        #
        # #1937, third CI round: when the anchor missed but ruby DID speak,
        # say what it saw -- the invoked path and whatever path ruby's own
        # output appears to name -- instead of leaving the next reader to
        # guess the transform from an assertion failure alone.
        errors = [{"line": None, "col": None, "severity": "error",
                   "code": "adapter",
                   "msg": _anchor_miss_message(
                       file, output,
                       tool_fault("ruby -c", result.returncode, output))}]

    emit({"tool": "ruby-check", "file": file, "ok": False, "count": len(errors),
          "errors": errors, "duration_ms": duration})


if __name__ == "__main__":
    # Was a hand-rolled `try/except` here, the only complete net in the tree.
    # It published `str(exc)` alone, so a `KeyError()` or a `RecursionError` --
    # both of which stringify to nothing -- produced a row with a blank reason,
    # and no exception ever named its own class. `guard_main` measures its own
    # elapsed, which is why `_STARTED` is gone (#1683, #1697).
    guard_main(TOOL, main)
