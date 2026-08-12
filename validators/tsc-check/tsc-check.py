#!/usr/bin/env python3
"""tsc-check validator adapter — TypeScript syntax check via tsc --noEmit.

Requires tsc on PATH. Absent, this reports the third state — `skipped` with the
reason — rather than the `ok: true` it emitted until #1202, which was a clean
verdict about a file nothing type-checked. Name this validator in
`$SUPERTOOL_REQUIRE_VALIDATORS` to turn that absence into a loud error instead.

**`--pretty false` is load-bearing, not tidiness (#1499).** TypeScript 5.x
defaults to pretty output whether or not stdout is a tty: ANSI-coloured, four
lines per diagnostic, a caret rule under the offending column, and a trailing
`Found N errors` tally. Its shape is `file:line:col - error TSxxxx: msg`, which
is not the `file(line,col): error TSxxxx: msg` the parse below reads — so
stripping the colour off it would not make it parseable. Only asking for the
plain form does. The strip is still applied, because it is `--pretty false` that
happens to remove the colour today and nothing tsc documents guarantees the two
stay coupled; a `tsc` shim, or a future default, can colour the plain form too.

Usage:  tsc-check.py <file>
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
from refusal import absent
from linebreaks import split_lines

TOOL = "tsc-check"
INSTALL_HINT = ("tsc not found on PATH — this file was NOT type-checked "
                "(`npm install -g typescript`)")


# Budget for the one tool spawn below. A module constant rather than a literal
# in the call so the decline can name it: a caller reading "timeout" cannot
# tell a hung compiler from a busy machine, and the number is the first thing
# they need to decide which (#658).
TIMEOUT_S = 30

# CSI sequences, OSC strings (BEL- or ST-terminated) and the two-character
# escapes. Not just SGR colour: an OSC that retitles the reader's terminal is
# exactly the kind of thing an adapter must not republish into a `msg`.
ANSI_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])"
)


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def emit(d: dict) -> None:
    print(json.dumps(d))


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": "tsc-check", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return

    file = sys.argv[1]
    start = time.time()

    if not shutil.which("tsc"):
        emit(absent(TOOL, file, INSTALL_HINT,
                    int((time.time() - start) * 1000)))
        return

    try:
        result = subprocess.run(
            ["tsc", "--noEmit", "--skipLibCheck", "--pretty", "false", file],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        # `which` said yes and exec said no — a PATH entry that vanished
        # between the two, or a name that resolves to something unrunnable.
        # Still an absent tool, so still the third state.
        emit(absent(TOOL, file, "tsc on PATH but could not be executed — "
                                "this file was NOT type-checked",
                    int((time.time() - start) * 1000)))
        return
    except subprocess.TimeoutExpired:
        # See hadolint.py for why this is a finding rather than a skip, and
        # why its absence was worse than a wrong verdict: an escaping
        # TimeoutExpired leaves stdout empty and the caller crashes on
        # json.loads with nothing naming the tool or the budget. `tsc` is the
        # likeliest of the three to reach its budget honestly — a cold
        # TypeScript compile on a cold runner is not fast — which makes
        # saying so, rather than dying, worth more here than anywhere.
        emit({"tool": "tsc-check", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter",
                          "msg": f"timeout — tsc did not return within {TIMEOUT_S}s; "
                                 "the file was NOT checked"}],
              "duration_ms": int((time.time() - start) * 1000)})
        return

    duration = int((time.time() - start) * 1000)

    if result.returncode == 0:
        emit({"tool": "tsc-check", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": duration})
        return

    # Parse tsc output: "file(line,col): error TSxxxx: message" — the shape
    # `--pretty false` above is what guarantees.
    errors = []
    pattern = re.compile(r"^(?:.*?)\((\d+),(\d+)\):\s+(\w+)\s+(TS\d+):\s+(.+)$")
    output = strip_ansi(result.stdout + result.stderr).strip()
    for line in split_lines(output):
        m = pattern.match(line)
        if m:
            lineno, col, severity, code, msg = m.groups()
            ln = int(lineno)
            err = {
                "line": ln,
                "col": int(col),
                "severity": severity if severity in ("error", "warning") else "error",
                "code": code,
                "msg": msg.strip()[:300],
            }
            err.update(context_fields(file, ln))
            errors.append(err)

    # tsc objected and nothing in what it said could be placed in this file.
    # `code: "adapter"` rather than `"syntax"`, which asserted a syntax error had
    # been found here — a claim this arm cannot make, published with `line: null`
    # and `count: 1` however many diagnostics the dump actually held (#1499). The
    # core reads `adapter` on every error as "no verdict was obtained"
    # (`_validator_not_checked`), which is the third state, reached without the
    # `skipped` that would drop `errors` and lose tsc's objection with it —
    # `validators/SCHEMA.md` §"A located diagnostic still has to be about *this*
    # file (#754)".
    #
    # Reached with `output` empty too. `ok: false, count: 0, errors: []` was a
    # verdict of "not clean" carrying nothing to act on, and one the core could
    # not recognise as an absence either, because it looks for the reason on an
    # error that was not there.
    if not errors:
        excerpt = " ".join(output.split())[:200] if output else "no output"
        errors = [{"line": None, "col": None, "severity": "error",
                   "code": "adapter",
                   "msg": f"tsc exited {result.returncode} and its output could "
                          f"not be parsed — this file was NOT type-checked: "
                          f"{excerpt}"}]

    emit({"tool": "tsc-check", "file": file, "ok": False, "count": len(errors),
          "errors": errors, "duration_ms": duration})


if __name__ == "__main__":
    main()
