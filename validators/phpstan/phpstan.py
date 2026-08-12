#!/usr/bin/env python3
"""phpstan validator adapter. Emits SCHEMA.md JSON.

Usage: phpstan.py <file>

Env vars:
  PHPSTAN_BIN      phpstan binary (default: phpstan)
  PHPSTAN_CONFIG   path to neon config file (optional, adds -c FILE)
  PHPSTAN_MEMORY   PHP memory_limit (default: 1G)
  PHPSTAN_LEVEL    analysis level (optional, adds --level N)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import pathlib
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "common"))
from source_context import context_fields
from refusal import is_refusal, skipped
from linebreaks import split_lines

# Extra refusal substrings (comma-separated), opt-in per repo.
SKIP_PATTERNS_ENV = "PHPSTAN_SKIP_PATTERNS"


def first_line(text: str) -> str:
    """The tool's own words, so the reader can fix the config that caused it."""
    for line in split_lines(text or ""):
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def refusal_line(text: str) -> str:
    """The line that actually declined, not whatever printed first.

    A real refusal arrives behind a `Note: Using configuration file ...`
    preamble. Quoting the preamble as the skip reason would name the config
    file and say nothing about why the file went unanalysed.
    """
    for line in split_lines(text or ""):
        stripped = line.strip()
        if stripped and is_refusal(stripped, SKIP_PATTERNS_ENV):
            return stripped
    return first_line(text)


def emit(obj: dict) -> None:
    print(json.dumps(obj))


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({
            "tool": "phpstan", "file": "", "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": "no file arg"}],
            "duration_ms": 0,
        })
        return

    file = sys.argv[1]

    phpstan_bin = os.environ.get("PHPSTAN_BIN", "phpstan")
    phpstan_memory = os.environ.get("PHPSTAN_MEMORY", "1G")
    phpstan_config = os.environ.get("PHPSTAN_CONFIG", "")
    phpstan_level = os.environ.get("PHPSTAN_LEVEL", "")

    # Guard: binary must exist
    if not shutil.which(phpstan_bin) and not (pathlib.Path(phpstan_bin).exists() and os.access(phpstan_bin, os.X_OK)):
        emit({
            "tool": "phpstan", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": f"PHPSTAN_BIN not found: {phpstan_bin}"}],
            "duration_ms": 0,
        })
        return

    cmd = ["php", f"-d", f"memory_limit={phpstan_memory}", phpstan_bin, "analyse"]
    if phpstan_config:
        cmd += ["-c", phpstan_config]
    if phpstan_level:
        cmd += ["--level", phpstan_level]
    cmd += ["--no-progress", "--error-format=json", file]

    start = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        dur = int((time.time() - start) * 1000)
        emit({
            "tool": "phpstan", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": "php binary not found"}],
            "duration_ms": dur,
        })
        return
    except subprocess.TimeoutExpired:
        emit({
            "tool": "phpstan", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": "timeout"}],
            "duration_ms": 120000,
        })
        return
    dur = int((time.time() - start) * 1000)

    raw = r.stdout.strip()
    try:
        data = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        data = None

    if data is None:
        # No parseable result. PHPStan reached no verdict about this file, and
        # the old default here ({"file_errors": 0}) turned that into `ok: true`
        # — a green meaning "I analysed nothing", indistinguishable from one
        # meaning "I analysed it and it is fine" (#263). Route it through the
        # three-state contract instead: a refusal we recognise is `skipped`, an
        # exit we cannot explain stays an error, and only a genuinely quiet
        # success keeps the clean verdict.
        combined = os.linesep.join([r.stdout or "", r.stderr or ""])
        if is_refusal(combined, SKIP_PATTERNS_ENV):
            reason = refusal_line(combined) or "phpstan declined to analyse"
            emit(skipped("phpstan", file, reason, dur))
            return
        if raw:
            emit({
                "tool": "phpstan", "file": file, "ok": False, "count": 1,
                "errors": [{"line": None, "col": None, "severity": "error",
                            "code": "adapter", "msg": "phpstan output not json"}],
                "duration_ms": dur,
            })
            return
        if r.returncode != 0:
            detail = first_line(r.stderr) or "no output"
            emit({
                "tool": "phpstan", "file": file, "ok": False, "count": 1,
                "errors": [{"line": None, "col": None, "severity": "error",
                            "code": "adapter",
                            "msg": f"phpstan produced no result (exit {r.returncode}): {detail}"}],
                "duration_ms": dur,
            })
            return
        data = {"totals": {"file_errors": 0}, "files": {}}

    count = int(data.get("totals", {}).get("file_errors", 0))
    errors = []
    for fdata in data.get("files", {}).values():
        for m in fdata.get("messages", []):
            line = m.get("line")
            errors.append({
                "line": line,
                "col": None,
                "severity": "error",
                "code": m.get("identifier"),
                "msg": m.get("message", ""),
                **context_fields(file, line),
            })

    emit({
        "tool": "phpstan",
        "file": file,
        "ok": count == 0,
        "count": count,
        "errors": errors,
        "duration_ms": dur,
    })


if __name__ == "__main__":
    main()
