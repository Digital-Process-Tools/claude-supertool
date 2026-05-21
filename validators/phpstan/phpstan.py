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
from source_context import source_context


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
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
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
        data = json.loads(raw) if raw else {"totals": {"file_errors": 0}, "files": {}}
    except json.JSONDecodeError:
        emit({
            "tool": "phpstan", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": "phpstan output not json"}],
            "duration_ms": dur,
        })
        return

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
                "source_context": source_context(file, line),
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
