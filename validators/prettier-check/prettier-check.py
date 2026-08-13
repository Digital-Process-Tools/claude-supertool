#!/usr/bin/env python3
"""prettier-check validator adapter. Emits SCHEMA.md JSON.

Usage: prettier-check.py <file>

Env vars:
  PRETTIER_BIN         prettier binary (default: prettier)
  PRETTIER_CONFIG      path to config file (optional, adds --config FILE)
  PRETTIER_IGNORE_PATH path to ignore file (optional, adds --ignore-path FILE)
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
from refusal import skipped

TOOL = "prettier-check"

# Budget for each spawn below. Named so a decline can quote it: a reader who
# sees "timeout" cannot tell a hung prettier from a busy machine (#658).
TIMEOUT_S = 15

# What an ignored file looks like, and why the clean arm needs a second question.
#
# prettier honours `.prettierignore` (and `--ignore-path`) for a path handed to
# it explicitly: it opens nothing, prints "All matched files use Prettier code
# style!" and exits 0 — byte for byte what a correctly formatted file produces
# (measured, prettier 3.6.2). So a zero exit is two different facts wearing the
# same output, and the only way to tell them apart is to ask prettier which of
# them this was.
#
# `prettier --file-info FILE` is that question: it resolves the same ignore
# files and answers `{"ignored": true|false, "inferredParser": ...}`. Asking
# prettier beats reimplementing the answer here — ignore resolution is
# gitignore-syntax over a file whose location is itself configurable.
#
# It costs one extra spawn and only on the arm where the answer is ambiguous:
# a run that reported a formatting difference has demonstrably read the file.
# The probe is handed the same `--config`/`--ignore-path` flags as the check,
# because a probe resolving a different ignore set answers about a different
# run.
#
# It is bounded by what is LEFT of TIMEOUT_S rather than given a fresh one, so
# this adapter still finishes inside the one budget its registration is set
# against. Two full walls would put the worst case at 2x the `"timeout": 15`
# in `.supertool.example.json`, and the core kills the adapter at its own
# wall: the caller would get `NOT CHECKED (timed out)` naming nothing, where a
# probe that runs out of clock declines and says which question went
# unanswered (`docs/validators.md`, on html-check's deliberate headroom).
IGNORED_REASON = ("prettier declined to check this file — it matched an ignore "
                  "pattern (`.prettierignore`, or the `--ignore-path` file); "
                  "`prettier --file-info` on this path answers "
                  '`"ignored": true`')

UNATTRIBUTABLE_REASON = ("prettier exited 0 and `prettier --file-info` could "
                         "not say whether the file is ignored, so this run is "
                         "not a verdict about it")


def emit(obj: dict) -> None:
    print(json.dumps(obj))


def _is_ignored(file: str, prettier_bin: str, flags: list,
                budget: float) -> "bool | None":
    """Was this file in scope? `None` when the question itself failed.

    `None` is not `False`. A probe that could not run leaves the zero exit
    unattributable, and publishing `ok` over it is the fabrication this arm
    exists to prevent — the caller gets the third state with the reason.
    """
    try:
        r = subprocess.run([prettier_bin, "--file-info", file] + flags,
                           capture_output=True, text=True,
                           timeout=max(1.0, budget),
                           encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    try:
        info = json.loads((r.stdout or "").strip())
    except ValueError:
        return None
    if not isinstance(info, dict) or not isinstance(info.get("ignored"), bool):
        return None
    return info["ignored"]


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({
            "tool": "prettier-check", "file": "", "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": "no file arg"}],
            "duration_ms": 0,
        })
        return

    file = sys.argv[1]
    start = time.time()

    prettier_bin = os.environ.get("PRETTIER_BIN", "prettier")
    prettier_config = os.environ.get("PRETTIER_CONFIG", "")
    prettier_ignore_path = os.environ.get("PRETTIER_IGNORE_PATH", "")

    if not shutil.which(prettier_bin) and not (
        pathlib.Path(prettier_bin).exists()
        and os.access(prettier_bin, os.X_OK)
    ):
        emit({
            "tool": "prettier-check", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": f"PRETTIER_BIN not found: {prettier_bin}"}],
            "duration_ms": int((time.time() - start) * 1000),
        })
        return

    flags = []
    if prettier_config:
        flags += ["--config", prettier_config]
    if prettier_ignore_path:
        flags += ["--ignore-path", prettier_ignore_path]
    cmd = [prettier_bin, "--check", file] + flags

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_S, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        dur = int((time.time() - start) * 1000)
        emit({
            "tool": "prettier-check", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": f"prettier binary not found: {prettier_bin}"}],
            "duration_ms": dur,
        })
        return
    except subprocess.TimeoutExpired:
        emit({
            "tool": "prettier-check", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter",
                        "msg": f"timeout — prettier did not return within "
                               f"{TIMEOUT_S}s; the file was NOT checked"}],
            "duration_ms": TIMEOUT_S * 1000,
        })
        return

    dur = int((time.time() - start) * 1000)

    # prettier --check exits 0 if file is formatted, 1 if it needs formatting
    # — and also when it never opened the file at all, which is the case the
    # probe above exists to separate (#1601). The duration is recomputed
    # afterwards: one that stops before the probe under-reports what the caller
    # waited for.
    if r.returncode == 0:
        ignored = _is_ignored(file, prettier_bin, flags,
                              TIMEOUT_S - (time.time() - start))
        if ignored is not False:
            emit(skipped(TOOL, file,
                         IGNORED_REASON if ignored else UNATTRIBUTABLE_REASON,
                         int((time.time() - start) * 1000)))
            return
        emit({"tool": "prettier-check", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": int((time.time() - start) * 1000)})
        return

    emit({
        "tool": "prettier-check",
        "file": file,
        "ok": False,
        "count": 1,
        "errors": [{"line": None, "col": None, "severity": "error",
                    "code": "formatting",
                    "msg": f"file needs formatting (run: {prettier_bin} --write {file})"}],
        "duration_ms": dur,
    })


if __name__ == "__main__":
    main()
