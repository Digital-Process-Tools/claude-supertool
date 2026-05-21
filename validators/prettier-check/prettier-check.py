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
import shutil
import subprocess
import sys
import time


def emit(obj: dict) -> None:
    print(json.dumps(obj))


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
        __import__("pathlib").Path(prettier_bin).exists()
        and os.access(prettier_bin, os.X_OK)
    ):
        emit({
            "tool": "prettier-check", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": f"PRETTIER_BIN not found: {prettier_bin}"}],
            "duration_ms": int((time.time() - start) * 1000),
        })
        return

    cmd = [prettier_bin, "--check", file]
    if prettier_config:
        cmd += ["--config", prettier_config]
    if prettier_ignore_path:
        cmd += ["--ignore-path", prettier_ignore_path]

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
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
                        "code": "adapter", "msg": "timeout"}],
            "duration_ms": 15000,
        })
        return

    dur = int((time.time() - start) * 1000)

    # prettier --check exits 0 if file is formatted, 1 if it needs formatting
    if r.returncode == 0:
        emit({"tool": "prettier-check", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": dur})
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
