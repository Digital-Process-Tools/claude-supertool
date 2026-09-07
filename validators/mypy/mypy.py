#!/usr/bin/env python3
"""mypy validator adapter — Python type-check via `mypy --output json`.

Requires `mypy` on PATH. Absent, this reports the third state — `skipped`
with the reason, never a fabricated `ok: true` about a file nothing
type-checked (docs/validators.md, "Declining instead of guessing"; the
eleven-instance defect list in #669). Name this validator in
`$SUPERTOOL_REQUIRE_VALIDATORS` to turn that absence into a loud error.

`mypy` and `pyright` are deliberately both offered rather than one replacing
the other — #669's own table: "Different findings, not a subset. Teams that
run both do so deliberately; pyright is stricter on inference, mypy on
plugin-typed libraries." This adapter mirrors `pyright.py`'s shape (same
three-state contract, same JSON-first parsing, same crash net) so the two
read as a pair rather than as two designs.

Usage:  mypy.py <file>

Output shape matches the supertool validator SCHEMA:
  {tool, file, ok, count, errors[], duration_ms}

Note on scope, and the one place mypy's shape differs from pyright's: `mypy
--output json` prints one JSON object **per line**, one per diagnostic,
rather than pyright's single JSON envelope — an empty stdout with an errorless
exit is itself the "no diagnostics" case, not an absence. `--no-error-summary`
suppresses the trailing "Found N errors" line that would otherwise not be
valid JSON on its own line, and `--cache-dir` is pinned to `os.devnull`
(mypy's own documented way to disable the cache; checked against mypy's own
source, `main.py`: `if options.cache_dir == os.devnull`) so invoking this
adapter does not leave a `.mypy_cache/` directory behind in whatever the
caller's cwd happens to be.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "common"))
from source_context import context_fields
from refusal import absent, guard_main
from linebreaks import split_lines

TOOL = "mypy"
INSTALL_HINT = ("mypy not found on PATH — this file was NOT type-checked "
                "(`pip install mypy`)")

#: How this adapter's `count` relates to `errors` (validators/SCHEMA.md, #1728).
#:
#: `total`: every verdict emitted below writes `count = len(errors)` -- there
#: is no separate accounting that excludes an `adapter`-coded stall row from
#: the count, the way `phpstan`'s `measured` convention does. Nothing here
#: mixes a stall row into the same payload as real findings today (a crash or
#: a non-JSON line short-circuits before any real finding is collected), but
#: the declaration is about what `count` COUNTS, not about today's inputs --
#: same reasoning cargo-check's own COUNT_CONTRACT gives for the adapter it
#: shares this convention with.
#:
#: `errors_truncated: False`: nothing here caps the list -- every diagnostic
#: mypy's `--output json` prints for the file is collected.
COUNT_CONTRACT = {"count_basis": "total", "errors_truncated": False}


def emit(d: dict) -> None:
    print(json.dumps(d))


def _skip(file: str, start: float, reason: str) -> None:
    emit(absent(TOOL, file, reason, int((time.time() - start) * 1000)))


def _adapter_error(file: str, duration: int, msg: str) -> dict:
    return {"tool": TOOL, "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": msg[:300]}],
            "duration_ms": duration, **COUNT_CONTRACT}


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit(_adapter_error("", 0, "no file arg"))
        return

    file = sys.argv[1]
    start = time.time()

    if not shutil.which("mypy"):
        _skip(file, start, INSTALL_HINT)
        return

    try:
        result = subprocess.run(
            ["mypy", "--output", "json", "--no-error-summary",
             "--no-color-output", "--cache-dir", os.devnull, file],
            capture_output=True,
            text=True,
            timeout=60, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        # `which` said yes and exec said no. Still an absent tool, so still the
        # third state — and still escalatable.
        _skip(file, start, "mypy on PATH but could not be executed")
        return
    except subprocess.TimeoutExpired:
        # Loud, not a skip: the binary exists and was invoked, so a hang is a
        # validator failure, same reasoning as pyright.py's twin branch —
        # `code: "adapter"` is the reserved spelling for "no verdict was
        # obtained" (SCHEMA.md), not a finding about the file's types.
        emit(_adapter_error(
            file, int((time.time() - start) * 1000),
            "timeout — mypy did not return within 60s; "
            "the file was NOT type-checked"))
        return

    duration = int((time.time() - start) * 1000)

    raw = result.stdout.strip()
    if not raw:
        if result.returncode not in (0, 1):
            # mypy exits 2 for a fatal/config error (missing target, unreadable
            # file, bad flags) — those write plain text on stdout/stderr, never
            # JSON, so an empty `raw` here is a real failure to report, not a
            # clean file. Surfaced as an adapter error rather than silently
            # matching the "no diagnostics" empty-stdout shape below (#669) —
            # conflating the two is exactly the absence-as-presence class this
            # repo keeps re-filing.
            stderr = (result.stderr or "").strip()
            emit(_adapter_error(
                file, duration,
                stderr or f"mypy exited {result.returncode} with no output"))
            return
        # Exit 0 (clean) or 1 (findings reported — but mypy still prints one
        # JSON line per finding, so exit 1 with empty stdout should not
        # happen) with nothing on stdout: no diagnostics, file is clean.
        emit({"tool": TOOL, "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": duration, **COUNT_CONTRACT})
        return

    errors = []
    for line in split_lines(raw):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError as e:
            # A line mypy did not emit as its own documented JSON shape. Stop
            # trusting the rest of the run rather than silently dropping the
            # unparseable line and reporting only what parsed clean — that is
            # exactly the "some information looks like all of it" pattern
            # this module's absence class is about.
            emit(_adapter_error(
                file, duration,
                f"mypy produced non-JSON output: {e}: {line}"))
            return
        sev = d.get("severity", "error")
        if sev not in ("error", "warning"):
            continue
        line_no = d.get("line")
        col_no = d.get("column")
        # `isinstance`, not truthiness: mypy never emits a real diagnostic's
        # `line` as 0 (unset/synthetic positions are its own -1 sentinel, per
        # `mypy/nodes.py`'s `Context`), but a truthiness check would silently
        # drop a genuine `line: 0` were that ever to change, the same way a
        # bare `if col_no` would treat a genuine leftmost column 0 as absent —
        # kept symmetric with the guard already used for `col_no` below.
        line_no = int(line_no) if isinstance(line_no, int) else None
        # mypy's JSON `column` is 0-indexed (checked directly: a bare
        # `undefined_name` on its own line reports column 0), same
        # convention as pyright's `range.start.character` — both get the
        # same +1 to match this repo's 1-indexed `source_context`/SCHEMA.
        col_no = int(col_no) + 1 if isinstance(col_no, int) else None
        msg = (d.get("message") or "").strip()
        msg = " · ".join(msg.splitlines())[:300]
        err = {
            "line": line_no,
            "col": col_no,
            "severity": sev,
            "code": d.get("code") or "mypy",
            "msg": msg,
        }
        if line_no is not None:
            err.update(context_fields(file, line_no))
        errors.append(err)

    ok = len(errors) == 0
    emit({"tool": TOOL, "file": file, "ok": ok, "count": len(errors),
          "errors": errors, "duration_ms": duration, **COUNT_CONTRACT})


if __name__ == "__main__":
    guard_main(TOOL, main)
