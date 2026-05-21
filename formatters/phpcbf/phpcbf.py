#!/usr/bin/env python3
"""phpcbf formatter adapter. Emits SCHEMA.md JSON.

Runs phpcbf --standard=PSR12 on the target file and computes before/after
line diff to populate metrics.lines_added / lines_removed.

Usage: phpcbf.py <file>

Env vars:
  PHPCBF_BIN       phpcbf binary (default: phpcbf)
  PHPCBF_STANDARD  coding standard (default: PSR12)
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
            "tool": "phpcbf", "file": "", "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": "no file arg"}],
            "duration_ms": 0,
            "metrics": {"lines_added": 0, "lines_removed": 0},
        })
        return

    file = sys.argv[1]
    phpcbf_bin = os.environ.get("PHPCBF_BIN", "phpcbf")
    phpcbf_standard = os.environ.get("PHPCBF_STANDARD", "PSR12")

    if not shutil.which(phpcbf_bin) and not (
        os.path.isfile(phpcbf_bin) and os.access(phpcbf_bin, os.X_OK)
    ):
        emit({
            "tool": "phpcbf", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter",
                        "msg": f"PHPCBF_BIN not found: {phpcbf_bin}"}],
            "duration_ms": 0,
            "metrics": {"lines_added": 0, "lines_removed": 0},
        })
        return

    try:
        before = open(file, encoding="utf-8", errors="replace").read()
    except OSError as e:
        emit({
            "tool": "phpcbf", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": f"cannot read file: {e}"}],
            "duration_ms": 0,
            "metrics": {"lines_added": 0, "lines_removed": 0},
        })
        return

    cmd = [phpcbf_bin, f"--standard={phpcbf_standard}", file]

    start = time.time()
    try:
        # phpcbf exits 1 when it makes fixes, 0 when nothing to fix, 2+ on error
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        emit({
            "tool": "phpcbf", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": "timeout after 60s"}],
            "duration_ms": 60000,
            "metrics": {"lines_added": 0, "lines_removed": 0},
        })
        return
    except (FileNotFoundError, OSError) as e:
        dur = int((time.time() - start) * 1000)
        emit({
            "tool": "phpcbf", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": str(e)}],
            "duration_ms": dur,
            "metrics": {"lines_added": 0, "lines_removed": 0},
        })
        return

    dur = int((time.time() - start) * 1000)

    # phpcbf: exit 0 = no fixes needed, exit 1 = fixes applied, exit 2+ = error
    if r.returncode >= 2:
        msg = (r.stderr.strip() or r.stdout.strip())[:500]
        emit({
            "tool": "phpcbf", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "phpcbf", "msg": msg}],
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
        "tool": "phpcbf",
        "file": file,
        "ok": True,
        "count": 0,
        "errors": [],
        "duration_ms": dur,
        "metrics": {"lines_added": added, "lines_removed": removed},
    })


if __name__ == "__main__":
    main()
