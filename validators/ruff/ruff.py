#!/usr/bin/env python3
"""ruff validator adapter — Python lint via `ruff check --output-format json`.

Requires `ruff` on PATH (`pip install ruff`). Absent, this reports the third
state — `skipped` with the reason — and never `ok` (validators/SCHEMA.md,
"Skipped: the third state"). A linter nobody installed has said nothing about
the file, and saying "clean" on its behalf is the one failure mode this
directory has filed eleven times.

Usage:  ruff.py <file>

**The ruleset is not this adapter's business.** `ruff check` resolves its
configuration by walking up from the file it was handed, so a project's own
`pyproject.toml`/`ruff.toml` decides what is reported, exactly as it would if
the developer ran ruff by hand. No `--select` is passed here: an adapter that
hard-coded one would report findings the project has not adopted, and the
first thing anyone would do about that is switch the validator off. This
repo's own choice lives in `pyproject.toml` under `[tool.ruff.lint]` and is
pinned by `tests/test_validators_ruff.py`.

`rollback_on_fail` is false in every registration of this validator, and that
is not a default anyone should flip. A lint finding is not a broken file:
reverting a good edit because it landed next to an unused import destroys work
to fix nothing. Contrast `py-compile`, where a failure means the file no
longer parses and rollback is the whole point.
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
from refusal import skipped, tool_fault

TOOL = "ruff"

# Budget for the one spawn below. Named so the decline can quote it: a reader
# who sees "timeout" cannot tell a hung linter from a busy machine, and the
# number is the first thing they need in order to decide (#658). Generous for
# a tool whose selling point is milliseconds — this is a hang-guard, not a
# performance assertion.
TIMEOUT_S = 30

# `ruff check` exit codes: 0 clean, 1 violations found, 2 ruff itself failed
# (bad config, unreadable path, unknown rule). Only 0 and 1 carry a verdict
# about the file; 2 is a fault someone has to fix and stays an `adapter` error.
RC_CLEAN = 0
RC_FINDINGS = 1


def emit(d: dict) -> None:
    print(json.dumps(d))


def _adapter_error(file: str, msg: str, dur_ms: int) -> None:
    emit({"tool": TOOL, "file": file, "ok": False, "count": 1,
          "errors": [{"line": None, "col": None, "severity": "error",
                      "code": "adapter", "msg": msg}],
          "duration_ms": dur_ms})


#: How ruff labels a file it could not parse or could not read, as opposed to
#: a rule it could evaluate. `invalid-syntax` is what 0.16 puts in `code`;
#: older releases leave `code` null for the same thing; `E902` is the IO
#: error. None of the three is a rule, so none of them can be selected,
#: ignored or suppressed inline, and all three mean the file is broken rather
#: than improvable.
_UNPARSEABLE = ("invalid-syntax", "E902", "E999")


def _is_unparseable(code: object) -> bool:
    return code is None or str(code) in _UNPARSEABLE


def _severity(code: object) -> str:
    """`error` only for the family that means the file did not parse.

    Ruff's own JSON carries a `severity`, and it is `error` on every row
    including a mutable default argument — so reading it would flatten the
    distinction this exists to draw. Everything under a correctness ruleset is
    a real finding the author should act on, but the file still runs, and this
    validator never rolls back; calling all of it `error` would overstate
    every row it ever prints.
    """
    return "error" if _is_unparseable(code) else "warning"


def _to_error(item: dict, file: str) -> dict:
    location = item.get("location") or {}
    line = location.get("row")
    col = location.get("column")
    code = item.get("code")
    msg = (item.get("message") or "").strip().replace("\n", " ")[:300]
    if _is_unparseable(code):
        # The message alone ("Expected a parameter or the end of the parameter
        # list") does not say the file failed to parse, and a reader scanning a
        # column of rule codes has nothing to match on.
        msg = f"syntax error: {msg}" if msg else "syntax error"
    err = {"line": line, "col": col, "severity": _severity(code),
           "code": code, "msg": msg}
    if isinstance(line, int):
        err["source_context"] = source_context(file, line)
    return err


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        _adapter_error("", "no file arg", 0)
        return

    file = sys.argv[1]
    start = time.time()

    if not shutil.which(TOOL):
        emit(skipped(TOOL, file, "ruff not found on PATH — pip install ruff",
                     int((time.time() - start) * 1000)))
        return

    cmd = [TOOL, "check", "--output-format", "json", "--no-cache",
           "--force-exclude", "--quiet", file]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=TIMEOUT_S, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        # `which` said yes and exec said no — a PATH entry that vanished
        # between the two, or a name that resolves to something unrunnable.
        # Still an absent tool, so still the third state.
        emit(skipped(TOOL, file, "ruff on PATH but could not be executed",
                     int((time.time() - start) * 1000)))
        return
    except subprocess.TimeoutExpired:
        # The binary was found and started, so this is not a decline: it is a
        # validator failure, loud, with the budget named (docs/validators.md,
        # "Declining instead of guessing").
        _adapter_error(file, f"timeout — ruff did not return within {TIMEOUT_S}s; "
                             "the file was NOT checked",
                       int((time.time() - start) * 1000))
        return

    dur = int((time.time() - start) * 1000)

    # The located diagnostic, not the exit code, is what confirms ruff looked
    # at the file (#753) — and with `--output-format json` the diagnostic is
    # the JSON array itself. `--quiet` keeps ruff's summary line off stdout so
    # nothing but the array is there to parse; warnings (an invalid noqa
    # directive, a deprecated setting) go to stderr and are not findings.
    body = (r.stdout or "").strip()
    if r.returncode not in (RC_CLEAN, RC_FINDINGS) and not body:
        _adapter_error(file, tool_fault("ruff check", r.returncode,
                                        r.stderr or r.stdout or ""), dur)
        return

    try:
        items = json.loads(body) if body else []
    except ValueError:
        _adapter_error(file, tool_fault("ruff check", r.returncode,
                                        r.stdout or r.stderr or ""), dur)
        return

    if not isinstance(items, list):
        _adapter_error(file, tool_fault("ruff check", r.returncode,
                                        f"expected a JSON array, got "
                                        f"{type(items).__name__}"), dur)
        return

    errors = [_to_error(i, file) for i in items if isinstance(i, dict)]
    emit({"tool": TOOL, "file": file, "ok": not errors, "count": len(errors),
          "errors": errors, "duration_ms": dur})


if __name__ == "__main__":
    main()
