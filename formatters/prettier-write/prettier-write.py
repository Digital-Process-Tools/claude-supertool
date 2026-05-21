#!/usr/bin/env python3
"""prettier --write formatter adapter. Emits SCHEMA.md JSON.

Runs prettier --write on the target file and computes before/after line diff
to populate metrics.lines_added / lines_removed.

Usage: prettier-write.py <file>

Env vars:
  PRETTIER_BIN          prettier binary (default: prettier)
  PRETTIER_CONFIG       --config path (optional)
  PRETTIER_IGNORE_PATH  --ignore-path (optional)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from difflib import unified_diff


def emit(obj: dict) -> None:
    print(json.dumps(obj))


def _line_diff(before: str, after: str) -> tuple[int, int]:
    """Return (lines_added, lines_removed) between two file contents."""
    added = removed = 0
    for line in unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        n=0,
    ):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({
            "tool": "prettier-write", "file": "", "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": "no file arg"}],
            "duration_ms": 0,
            "metrics": {"lines_added": 0, "lines_removed": 0},
        })
        return

    file = sys.argv[1]
    prettier_bin = os.environ.get("PRETTIER_BIN", "prettier")
    prettier_config = os.environ.get("PRETTIER_CONFIG", "")
    prettier_ignore = os.environ.get("PRETTIER_IGNORE_PATH", "")

    if not shutil.which(prettier_bin) and not (
        os.path.isfile(prettier_bin) and os.access(prettier_bin, os.X_OK)
    ):
        emit({
            "tool": "prettier-write", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter",
                        "msg": f"PRETTIER_BIN not found: {prettier_bin}"}],
            "duration_ms": 0,
            "metrics": {"lines_added": 0, "lines_removed": 0},
        })
        return

    try:
        before = open(file, encoding="utf-8", errors="replace").read()
    except OSError as e:
        emit({
            "tool": "prettier-write", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": f"cannot read file: {e}"}],
            "duration_ms": 0,
            "metrics": {"lines_added": 0, "lines_removed": 0},
        })
        return

    cmd = [prettier_bin, "--write"]
    if prettier_config:
        cmd += ["--config", prettier_config]
    if prettier_ignore:
        cmd += ["--ignore-path", prettier_ignore]
    cmd.append(file)

    start = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        emit({
            "tool": "prettier-write", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": "timeout after 30s"}],
            "duration_ms": 30000,
            "metrics": {"lines_added": 0, "lines_removed": 0},
        })
        return
    except (FileNotFoundError, OSError) as e:
        dur = int((time.time() - start) * 1000)
        emit({
            "tool": "prettier-write", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": str(e)}],
            "duration_ms": dur,
            "metrics": {"lines_added": 0, "lines_removed": 0},
        })
        return

    dur = int((time.time() - start) * 1000)

    if r.returncode != 0:
        msg = (r.stderr.strip() or r.stdout.strip())[:500]
        emit({
            "tool": "prettier-write", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "prettier", "msg": msg}],
            "duration_ms": dur,
            "metrics": {"lines_added": 0, "lines_removed": 0},
        })
        return

    try:
        after = open(file, encoding="utf-8", errors="replace").read()
    except OSError:
        after = before

    added, removed = _line_diff(before, after)

    emit({
        "tool": "prettier-write",
        "file": file,
        "ok": True,
        "count": 0,
        "errors": [],
        "duration_ms": dur,
        "metrics": {"lines_added": added, "lines_removed": removed},
    })


if __name__ == "__main__":
    main()
