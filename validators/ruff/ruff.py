#!/usr/bin/env python3
"""ruff validator adapter — Python lint via `ruff check --output-format json`.

Requires `ruff` on PATH (`pip install ruff`). Absent, this reports the third
state — `skipped` with the reason — and never `ok` (validators/SCHEMA.md,
"Skipped: the third state"). A linter nobody installed has said nothing about
the file, and saying "clean" on its behalf is the one failure mode this
directory has filed eleven times.

Where that quiet is not acceptable — CI, where "not installed" means the gate
is not running — name this validator in `$SUPERTOOL_REQUIRE_VALIDATORS` and the
same absence becomes a loud `adapter` error instead. That routing is
`refusal.absent()`, and it did not reach this adapter until #1202: the switch
was inert here, and setting it was indistinguishable from not setting it.

Usage:  ruff.py <file>

**The ruleset is not this adapter's business.** `ruff check` resolves its
configuration by walking up from the file it was handed, so a project's own
`pyproject.toml`/`ruff.toml` decides what is reported, exactly as it would if
the developer ran ruff by hand. No `--select` is passed here: an adapter that
hard-coded one would report findings the project has not adopted, and the
first thing anyone would do about that is switch the validator off. This
repo's own choice lives in `pyproject.toml` under `[tool.ruff.lint]` and is
pinned by `tests/test_validators_ruff.py`.

**An `exclude` entry is honoured, and honouring it means saying so.** `ruff
check` is handed `--force-exclude`, so `[tool.ruff] exclude` applies to the
explicit path this adapter names; a match makes ruff exit 0 with an empty
array, which is what a clean file looks like too (#1587). The zero-finding arm
therefore asks a second question — `--show-files` — and reports `skipped` with
the pattern rather than a verdict about a file nothing opened. The alternative,
dropping the flag so an explicitly-named file is always linted, is what the
#1481 release gate does over a hand-built diff list; it is the wrong answer for
a post-edit validator, and `eslint` already declines an ignored file here.

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
from source_context import context_fields
from refusal import absent, skipped, tool_fault

TOOL = "ruff"

# Named once so the skip reason and the `$SUPERTOOL_REQUIRE_VALIDATORS`
# escalation quote the same sentence — a reader who set the variable and a
# reader who did not are looking at the same missing tool.
INSTALL_HINT = "ruff not found on PATH — pip install ruff"

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

# What an excluded file looks like, and why it needs a second question.
#
# `--force-exclude` applies `[tool.ruff] exclude` to a path handed to ruff
# explicitly. When it matches, ruff opens nothing and exits 0 with an empty
# array — byte for byte what a clean file produces (#1587). So a zero-finding
# run is two different facts wearing the same output, and the only way to tell
# them apart is to ask ruff which files it would have checked.
#
# `--show-files` is that question: it resolves the same configuration and
# prints the paths that survive exclusion, one per line, nothing on a match.
# Asking ruff beats reimplementing the answer here — exclusion resolution
# walks up for `pyproject.toml`/`ruff.toml`, honours `extend-exclude`, and
# carries a default list (`.venv`, `build`, `node_modules`, …) that a hand
# written check would drift from on every ruff release.
#
# It costs one extra spawn (~50ms, measured against ruff 0.16.1 on macOS; not
# measured on Windows, where process creation is dearer) and only on the arm
# where the answer is ambiguous: a run that reported findings has demonstrably
# opened the file.
#
# The probe shares TIMEOUT_S rather than carrying a tighter budget of its own.
# A separate wall would let a loaded machine finish the lint and blow the
# probe, turning a real `ok` into a decline over nothing about the file.
#
# The reason names no single config key. `--force-exclude` enforces `exclude`,
# `extend-exclude` and ruff's built-in default list at once, and `--show-files`
# reports the outcome, not which of the three produced it — so a message
# blaming `[tool.ruff] exclude` sends a reader with an `extend-exclude` entry
# to a key they never wrote.
EXCLUDED_REASON = ("ruff declined to lint this file — it is excluded by the "
                   "ruff configuration resolved for it (`exclude`, "
                   "`extend-exclude`, or ruff's built-in defaults; "
                   "`ruff check --show-files` on this path lists nothing)")


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
        err.update(context_fields(file, line))
    return err


def _would_be_checked(file: str) -> bool | None:
    """Did ruff consider this file in scope? `None` when the question failed.

    `None` is not `True`. A probe that could not run leaves the zero-finding
    result unattributable, and publishing `ok` over it is the fabrication this
    whole change is about — the caller gets the third state with the reason
    instead.
    """
    try:
        r = subprocess.run([TOOL, "check", "--no-cache", "--force-exclude",
                            "--show-files", "--", file],
                           capture_output=True, text=True, timeout=TIMEOUT_S,
                           encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != RC_CLEAN:
        return None
    # Ruff prints the surviving paths on stdout and its "No Python files found
    # under the given path(s)" warning on stderr, so emptiness here is the
    # whole signal — and no path is compared, which is what keeps this from
    # becoming a separator question on Windows.
    return bool((r.stdout or "").strip())


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        _adapter_error("", "no file arg", 0)
        return

    file = sys.argv[1]
    start = time.time()

    if not shutil.which(TOOL):
        emit(absent(TOOL, file, INSTALL_HINT,
                    int((time.time() - start) * 1000)))
        return

    # `--`: `file` is argv[1] and containment gates where a path points, not
    # what it looks like, so a name beginning with `-` reaches here intact and
    # would be parsed as an option rather than opened. The failure is quiet in
    # both invocations -- ruff with no positional path reports nothing, the
    # scope probe then sees no surviving path, and the adapter emits `skipped`
    # blaming an exclude that is not there.
    cmd = [TOOL, "check", "--output-format", "json", "--no-cache",
           "--force-exclude", "--quiet", "--", file]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=TIMEOUT_S, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        # `which` said yes and exec said no — a PATH entry that vanished
        # between the two, or a name that resolves to something unrunnable.
        # Still an absent tool, so still the third state.
        emit(absent(TOOL, file, "ruff on PATH but could not be executed",
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

    if not errors and r.returncode == RC_CLEAN:
        in_scope = _would_be_checked(file)
        if in_scope is not True:
            reason = (EXCLUDED_REASON if in_scope is False else
                      "ruff reported nothing and `ruff check --show-files` "
                      "could not say whether the file is excluded, so this "
                      "run is not a verdict about it")
            emit(skipped(TOOL, file, reason,
                         int((time.time() - start) * 1000)))
            return

    # Recomputed: the scope probe above spawns ruff a second time, and a
    # duration that stops before it under-reports what the caller waited for.
    emit({"tool": TOOL, "file": file, "ok": not errors, "count": len(errors),
          "errors": errors, "duration_ms": int((time.time() - start) * 1000)})


if __name__ == "__main__":
    main()
