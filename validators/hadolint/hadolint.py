#!/usr/bin/env python3
"""hadolint validator adapter — Dockerfile lint via hadolint CLI.

Requires hadolint on PATH. Absent, this reports the third state — `skipped` with
the reason — rather than the `ok: true` it emitted until #1202, which was a clean
verdict about a Dockerfile nothing linted. Name this validator in
`$SUPERTOOL_REQUIRE_VALIDATORS` to turn that absence into a loud error instead.

Usage:  hadolint.py <file>
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
from path_anchor import anchor as _anchor

TOOL = "hadolint"
INSTALL_HINT = ("hadolint not found on PATH — this Dockerfile was NOT linted "
                "(`brew install hadolint`)")


# Budget for the one tool spawn below. A module constant rather than a literal
# in the call so the decline can name it: a caller reading "timeout" cannot
# tell a hung linter from a busy machine, and the number is the first thing
# they need to decide which (#658).
TIMEOUT_S = 30


# hadolint tty output: `file:line RULE severity: message`.
#
# Anchored on the invoked path itself (#1934) rather than a bare `.*?`: the
# non-greedy wildcard used to discard the path instead of matching it, so it
# bound to the *earliest* `:digit` run anywhere in the line — including one
# supplied by a Dockerfile filename crafted to contain its own
# `N RULE severity: ` sequence. Building the pattern from `file` means only
# a spelling of the path hadolint was actually invoked against can start a
# match (see `path_anchor.py`, #1937, for what "a spelling of" widened to
# after this comment was first written).
#
# Reasoned, not observed: this assumes hadolint echoes back the literal argv
# path unmodified, the way ruby/gofmt/xmllint were verified to do (see the
# siblings in this sweep) — hadolint was not installed on any machine this
# fix was written or reviewed on, so that assumption could not be checked
# against a real binary. actionlint was checked and turned out NOT to share
# it (it relativises against its own CWD instead; see actionlint.py). If
# hadolint turns out to behave like actionlint, the practical effect is not
# a re-opened #1934 — it is every located finding falling through to the
# unlocated `code: "lint"` branch below, a visible regression rather than a
# silent one.
#
# Tolerant of the spellings a real hadolint can echo that back in (#1937),
# whatever they turn out to be -- see validators/common/path_anchor.py.
def _pattern(file: str) -> re.Pattern[str]:
    return _anchor(file, r":(\d+)\s+((?:DL|SC)\d+)\s+(\w+):\s+(.+)$")


def parse_diagnostics(output: str, file: str) -> list[dict]:
    """Every located hadolint diagnostic about `file`.

    Extracted so the rule can be driven in process on every platform,
    matching the sibling adapters this class was found across (#1934).
    """
    pattern = _pattern(file)
    errors = []
    for line in split_lines(output):
        m = pattern.match(line)
        if m:
            lineno, code, severity, msg = m.groups()
            ln = int(lineno)
            err = {
                "line": ln,
                "col": None,
                "severity": severity if severity in ("error", "warning", "info", "style") else "error",
                "code": code,
                "msg": msg.strip()[:300],
            }
            err.update(context_fields(file, ln))
            errors.append(err)
    return errors


def emit(d: dict) -> None:
    print(json.dumps(d))


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": "hadolint", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return

    file = sys.argv[1]
    start = time.time()

    if not shutil.which("hadolint"):
        emit(absent(TOOL, file, INSTALL_HINT,
                    int((time.time() - start) * 1000)))
        return

    try:
        result = subprocess.run(
            ["hadolint", "--format", "tty", file],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        # `which` said yes and exec said no — a PATH entry that vanished
        # between the two, or a name that resolves to something unrunnable.
        # Still an absent tool, so still the third state.
        emit(absent(TOOL, file, "hadolint on PATH but could not be executed — "
                                "this Dockerfile was NOT linted",
                    int((time.time() - start) * 1000)))
        return
    except subprocess.TimeoutExpired:
        # Not a finding about the file, and not silence either. Without this
        # the exception escapes an adapter with no top-level handler, the
        # process dies on a traceback with **empty stdout**, and every caller
        # json.loads() that — so a slow linter surfaces as a JSONDecodeError
        # naming neither the tool nor the timeout. Same three-state collapse
        # #650 fixed for git: "could not answer" wearing the clothes of an
        # answer. It stays ok=False rather than becoming a skip, because the
        # binary was found and started (docs/validators.md — a post-spawn
        # failure is a finding with an `adapter` code, never a decline).
        emit({"tool": "hadolint", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter",
                          "msg": f"timeout — hadolint did not return within {TIMEOUT_S}s; "
                                 "the file was NOT checked"}],
              "duration_ms": int((time.time() - start) * 1000)})
        return

    duration = int((time.time() - start) * 1000)

    if result.returncode == 0:
        emit({"tool": "hadolint", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": duration})
        return

    output = (result.stdout + result.stderr).strip()
    errors = parse_diagnostics(output, file)

    if not errors and output:
        errors = [{"line": None, "col": None, "severity": "error",
                   "code": "lint", "msg": output[:300]}]

    emit({"tool": "hadolint", "file": file, "ok": False, "count": len(errors),
          "errors": errors, "duration_ms": duration})


if __name__ == "__main__":
    guard_main(TOOL, main)
