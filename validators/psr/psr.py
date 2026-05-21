#!/usr/bin/env python3
"""phpcs PSR-12 validator adapter. Emits SCHEMA.md JSON.

Usage: psr.py <file>

Env vars:
  PSR_BIN        phpcs binary (default: phpcs)
  PSR_STANDARD   coding standard (default: PSR12)
  PSR_EXCLUDE    comma-separated exclude paths (optional)
  PSR_SEVERITY   warning severity threshold (default: 9)
  PSR_EXTENSIONS file extensions to check (default: php)
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
            "tool": "psr", "file": "", "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": "no file arg"}],
            "duration_ms": 0,
        })
        return

    file = sys.argv[1]

    psr_bin = os.environ.get("PSR_BIN", "phpcs")
    psr_standard = os.environ.get("PSR_STANDARD", "PSR12")
    psr_exclude = os.environ.get("PSR_EXCLUDE", "")
    psr_severity = os.environ.get("PSR_SEVERITY", "9")
    psr_extensions = os.environ.get("PSR_EXTENSIONS", "php")

    # Guard: binary must exist
    if not shutil.which(psr_bin) and not (pathlib.Path(psr_bin).exists() and os.access(psr_bin, os.X_OK)):
        emit({
            "tool": "psr", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": f"PSR_BIN not found: {psr_bin}"}],
            "duration_ms": 0,
        })
        return

    cmd = [
        psr_bin,
        f"--standard={psr_standard}",
        f"--warning-severity={psr_severity}",
        f"--extensions={psr_extensions}",
        "--report=json",
        "--runtime-set", "ignore_warnings_on_exit", "true",
    ]
    if psr_exclude:
        cmd.append(f"--ignore={psr_exclude}")
    cmd.append(file)

    start = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        dur = int((time.time() - start) * 1000)
        emit({
            "tool": "psr", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": f"PSR_BIN not found: {psr_bin}"}],
            "duration_ms": dur,
        })
        return
    except subprocess.TimeoutExpired:
        emit({
            "tool": "psr", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": "timeout"}],
            "duration_ms": 120000,
        })
        return
    dur = int((time.time() - start) * 1000)

    raw = r.stdout.strip()
    try:
        data = json.loads(raw) if raw else {"totals": {"errors": 0, "warnings": 0}, "files": {}}
    except json.JSONDecodeError:
        emit({
            "tool": "psr", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": "phpcs output not json"}],
            "duration_ms": dur,
        })
        return

    errors = []
    for fdata in data.get("files", {}).values():
        for msg in fdata.get("messages", []):
            line = msg.get("line")
            col = msg.get("column")
            sev_raw = msg.get("type", "ERROR").upper()
            severity = "warning" if sev_raw == "WARNING" else "error"
            errors.append({
                "line": line,
                "col": col,
                "severity": severity,
                "code": msg.get("source"),
                "msg": msg.get("message", ""),
                "source_context": source_context(file, line),
            })

    count = len(errors)
    emit({
        "tool": "psr",
        "file": file,
        "ok": count == 0,
        "count": count,
        "errors": errors,
        "duration_ms": dur,
    })


if __name__ == "__main__":
    main()
