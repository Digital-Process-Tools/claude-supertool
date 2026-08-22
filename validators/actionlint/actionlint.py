#!/usr/bin/env python3
"""actionlint validator adapter — GitHub Actions workflow lint via actionlint CLI.

Requires actionlint on PATH. Absent, this reports the third state — `skipped`
with the reason — rather than an `ok: true` about a workflow nothing checked,
the same failure #1202 fixed across ten adapters. Name this validator in
`$SUPERTOOL_REQUIRE_VALIDATORS` to turn that absence into a loud error instead.

actionlint was not installed on the machine this adapter was written on
(#1798), so the absent path is the first thing anyone enabling this validator
hits, not an edge case.

Usage:  actionlint.py <file>
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
from refusal import absent, guard_main
from linebreaks import split_lines

TOOL = "actionlint"
INSTALL_HINT = ("actionlint not found on PATH — this workflow was NOT linted "
                "(`brew install actionlint`)")

# Budget for the one tool spawn below. A module constant rather than a literal
# in the call so the decline can name it (#658) — see hadolint.py, which this
# adapter mirrors.
TIMEOUT_S = 30

#: How this adapter's `count` relates to `errors` (validators/SCHEMA.md, #1728).
#:
#: `measured`: `count` is set to `len(errors)` from the same loop that builds
#: `errors` below, over actionlint's own diagnostic lines — it has never
#: counted an `adapter`-coded row. The two real-verdict emits below (clean, and
#: findings-present) never place an `adapter` row alongside a real finding: the
#: `adapter` code appears only in the absent-tool/timeout/no-arg/crash arms,
#: each of which is an exclusive emit carrying no findings of its own. So the
#: two never mix, and `count` counting only real findings is a statement this
#: constant makes rather than a coincidence of the current output.
#:
#: `errors_truncated: False`: the loop over `split_lines(output)` copies every
#: line `_LINE_RE` matches, plus the single unmatched-output fallback row when
#: none does — nothing here caps the list.
COUNT_CONTRACT = {"count_basis": "measured", "errors_truncated": False}

# actionlint's default text output: `file:line:col: message [rule]`. Not every
# line ends in a bracketed rule (a parse-level failure can omit it), so the
# rule group is optional and a match with no rule falls back to "syntax-check"
# rather than losing the whole line to the fallback branch below it.
_LINE_RE = re.compile(r"^(?:.*?):(\d+):(\d+):\s+(.+?)(?:\s+\[([\w-]+)\])?$")


def emit(d: dict) -> None:
    print(json.dumps(d))


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": TOOL, "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return

    file = sys.argv[1]
    start = time.time()

    if not shutil.which("actionlint"):
        emit(absent(TOOL, file, INSTALL_HINT,
                    int((time.time() - start) * 1000)))
        return

    try:
        result = subprocess.run(
            ["actionlint", "-no-color", "-oneline", "--", file],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        # `which` said yes and exec said no — a PATH entry that vanished
        # between the two, or a name that resolves to something unrunnable.
        # Still an absent tool, so still the third state (see hadolint.py).
        emit(absent(TOOL, file, "actionlint on PATH but could not be executed "
                                "— this workflow was NOT linted",
                    int((time.time() - start) * 1000)))
        return
    except subprocess.TimeoutExpired:
        # Not a finding about the file, and not silence either — see
        # hadolint.py and docs/validators.md for why this stays ok=False
        # rather than becoming a skip: the binary was found and started.
        emit({"tool": TOOL, "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter",
                          "msg": f"timeout — actionlint did not return within {TIMEOUT_S}s; "
                                 "the file was NOT checked"}],
              "duration_ms": int((time.time() - start) * 1000)})
        return

    duration = int((time.time() - start) * 1000)

    if result.returncode == 0:
        emit({"tool": TOOL, "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": duration, **COUNT_CONTRACT})
        return

    errors = []
    output = (result.stdout + result.stderr).strip()
    for line in split_lines(output):
        m = _LINE_RE.match(line)
        if m:
            lineno, col, msg, rule = m.groups()
            ln = int(lineno)
            err = {
                "line": ln,
                "col": int(col),
                "severity": "error",
                "code": rule or "syntax-check",
                "msg": msg.strip()[:300],
            }
            err.update(context_fields(file, ln))
            errors.append(err)

    if not errors and output:
        errors = [{"line": None, "col": None, "severity": "error",
                   "code": "lint", "msg": output[:300]}]

    emit({"tool": TOOL, "file": file, "ok": False, "count": len(errors),
          "errors": errors, "duration_ms": duration, **COUNT_CONTRACT})


if __name__ == "__main__":
    guard_main(TOOL, main)
