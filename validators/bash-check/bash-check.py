#!/usr/bin/env python3
"""bash-check validator adapter — bash syntax check via `bash -n`.

Stdlib only. Reference implementation per validators/SCHEMA.md.
Usage:  bash-check.py <file>
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "common"))
from source_context import context_fields
from refusal import guard_main, tool_fault
from linebreaks import split_lines
from quote_balance import unbalanced_quote_open

# `bash -n` reports a syntax finding as `file: line N: message`. Its other
# non-zero exits are the shell failing to get as far as parsing:
#
#   bash: x.sh: No such file or directory   exit 127
#   bash: x.sh: Permission denied           exit 126
#   .: .: is a directory                    exit 126
#   bash: --nope: invalid option + usage    exit 2
#
# 126 and 127 are the shell's own "could not execute" codes and can never be a
# statement about syntax. All four used to become one `code: "syntax"` error
# carrying the raw text — the usage dump included (#753). A located `: line N:`
# is the marker.


# `file: line N: message`. Extracted so the rule can be driven in process on
# every platform — the fake-binary fixture is POSIX-only (see
# tests/test_adapter_tool_vs_file_753.py).
DIAGNOSTIC = re.compile(r":\s*line\s+(\d+):\s*(.+)")


def _read_for_guess(file: str) -> str:
    """Best-effort read for `unbalanced_quote_open`, never a diagnostic path.

    An unreadable file is not this function's failure to report -- the
    diagnostic itself still stands on `context_fields`, which has its own
    `context_unavailable` state for exactly that case. This one just gives up
    on the guess quietly, by returning "", which `unbalanced_quote_open`
    treats as an empty file (no quote open anywhere).

    Known and left as-is: this duplicates the read `source_context()`
    (validators/common/source_context.py) already does for the same
    diagnostic, so a run producing at least one finding opens the file twice.
    Sharing one read between two independently-designed helper modules is a
    real fix, but it is not this one -- it would mean touching
    `source_context`'s own contract for a cost that only matters on files
    large enough for a second full read to register at all (#1810 review).
    """
    try:
        return pathlib.Path(file).read_text(errors="replace", encoding="utf-8")
    except OSError:
        return ""


def parse_diagnostics(out: str, file: str) -> list[dict]:
    """Every located diagnostic in bash's stderr. Empty means the shell never
    got as far as parsing.

    `bash -n`'s own `line` is exact about where its parser gave up, and stays
    untouched. An unterminated quote can leave that line far from the one that
    actually broke -- five lines and an indented `function` block, in the
    incident this exists for (#1810) -- so each finding also carries a
    `quote_open_guess` when a plain state-machine scan finds a quote still
    open by the time it reaches `line`. It is a GUESS, explicitly labelled:
    the scan knows nothing of here-docs or command substitution, so it can be
    wrong, and it is never folded into `line`, `col` or `msg`.
    """
    errors = []
    text = None
    for line in split_lines(out):
        m = DIAGNOSTIC.search(line)
        if m:
            ln = int(m.group(1))
            err = {"line": ln, "col": None, "severity": "error",
                   "code": "syntax", "msg": m.group(2).strip()[:200],
                   **context_fields(file, ln)}
            if text is None:
                text = _read_for_guess(file)
            if text:
                guess_line = unbalanced_quote_open(text, ln)
                if guess_line is not None and guess_line != ln:
                    err["quote_open_guess"] = {
                        "line": guess_line,
                        "note": ("best-effort: a quote opened here looks still "
                                 "unclosed by the reported line. Not itself a "
                                 "diagnostic -- `line` above is bash's own and "
                                 "exact, this is a guess (#1810)."),
                    }
            errors.append(err)
    return errors


def emit(d: dict) -> None:
    print(json.dumps(d))


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": "bash-check", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return
    file = sys.argv[1]
    start = time.time()
    try:
        r = subprocess.run(["bash", "-n", file],
                           capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        emit({"tool": "bash-check", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "bash binary not found"}],
              "duration_ms": int((time.time() - start) * 1000)})
        return
    except subprocess.TimeoutExpired:
        emit({"tool": "bash-check", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "timeout"}],
              "duration_ms": int((time.time() - start) * 1000)})
        return
    dur = int((time.time() - start) * 1000)
    if r.returncode == 0:
        emit({"tool": "bash-check", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": dur})
        return
    # stderr lines: "file: line N: msg" or "file: line N: syntax error near unexpected token ..."
    out = r.stderr or ""
    errors = parse_diagnostics(out, file)
    if not errors:
        errors = [{"line": None, "col": None, "severity": "error",
                   "code": "adapter",
                   "msg": tool_fault("bash -n", r.returncode, out)}]
    emit({"tool": "bash-check", "file": file, "ok": False, "count": len(errors),
          "errors": errors, "duration_ms": dur})


if __name__ == "__main__":
    guard_main("bash-check", main)
