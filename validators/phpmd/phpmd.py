#!/usr/bin/env python3
"""phpmd validator adapter. Emits SCHEMA.md JSON.

Usage: phpmd.py <file>

Env vars:
  PHPMD_BIN       phpmd binary (default: phpmd)
  PHPMD_RULESETS  comma-separated rulesets (default: cleancode,codesize,controversial,design,naming,unusedcode)
  PHPMD_FORMAT    output format (default: text)
  PHPMD_EXCLUDE   value for --exclude flag (optional)
"""
from __future__ import annotations

import json
import os
import re
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
            "tool": "phpmd", "file": "", "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": "no file arg"}],
            "duration_ms": 0,
        })
        return

    file = sys.argv[1]

    phpmd_bin = os.environ.get("PHPMD_BIN", "phpmd")
    phpmd_rulesets = os.environ.get("PHPMD_RULESETS", "cleancode,codesize,controversial,design,naming,unusedcode")
    phpmd_format = os.environ.get("PHPMD_FORMAT", "text")
    phpmd_exclude = os.environ.get("PHPMD_EXCLUDE", "")

    cmd = [phpmd_bin, file, phpmd_format, phpmd_rulesets, "--suffixes", "php,phtml"]
    if phpmd_exclude:
        cmd += ["--exclude", phpmd_exclude]

    start = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        dur = int((time.time() - start) * 1000)
        emit({
            "tool": "phpmd", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": f"phpmd binary not found: {phpmd_bin}"}],
            "duration_ms": dur,
        })
        return
    except subprocess.TimeoutExpired:
        emit({
            "tool": "phpmd", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": "timeout"}],
            "duration_ms": 120000,
        })
        return
    dur = int((time.time() - start) * 1000)

    raw = r.stdout or ""
    errors = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        # "path/to/file.php:42\tRuleName\tHuman message"
        m = re.match(r'^.+?:(\d+)\t([^\t]+)\t(.+)$', line)
        if m:
            lineno = int(m.group(1))
            rule = m.group(2).strip()
            msg = m.group(3).strip()
            errors.append({
                "line": lineno,
                "col": None,
                "severity": "warning",
                "code": rule,
                "msg": msg,
                "source_context": source_context(file, lineno),
            })
            continue
        # Fallback: "path/to/file.php:42  message" (space-separated, no rule column)
        m2 = re.match(r'^.+?:(\d+)\s+(.+)$', line)
        if m2:
            lineno = int(m2.group(1))
            msg = m2.group(2).strip()
            errors.append({
                "line": lineno,
                "col": None,
                "severity": "warning",
                "code": None,
                "msg": msg,
                "source_context": source_context(file, lineno),
            })

    count = len(errors)
    emit({
        "tool": "phpmd",
        "file": file,
        "ok": count == 0,
        "count": count,
        "errors": errors,
        "duration_ms": dur,
    })


if __name__ == "__main__":
    main()
