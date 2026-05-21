#!/usr/bin/env python3
"""php-cs-fixer formatter adapter. Emits SCHEMA.md JSON.

Runs php-cs-fixer fix on the target file and computes before/after line diff
to populate metrics.lines_added / lines_removed.

Usage: php-cs-fixer.py <file>

Env vars:
  PHPCSFIXER_BIN     php-cs-fixer binary (default: php-cs-fixer)
  PHPCSFIXER_CONFIG  --config path (optional)
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
            "tool": "php-cs-fixer", "file": "", "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": "no file arg"}],
            "duration_ms": 0,
            "metrics": {"lines_added": 0, "lines_removed": 0},
        })
        return

    file = sys.argv[1]
    phpcsfixer_bin = os.environ.get("PHPCSFIXER_BIN", "php-cs-fixer")
    phpcsfixer_config = os.environ.get("PHPCSFIXER_CONFIG", "")

    if not shutil.which(phpcsfixer_bin) and not (
        os.path.isfile(phpcsfixer_bin) and os.access(phpcsfixer_bin, os.X_OK)
    ):
        emit({
            "tool": "php-cs-fixer", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter",
                        "msg": f"PHPCSFIXER_BIN not found: {phpcsfixer_bin}"}],
            "duration_ms": 0,
            "metrics": {"lines_added": 0, "lines_removed": 0},
        })
        return

    try:
        before = open(file, encoding="utf-8", errors="replace").read()
    except OSError as e:
        emit({
            "tool": "php-cs-fixer", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": f"cannot read file: {e}"}],
            "duration_ms": 0,
            "metrics": {"lines_added": 0, "lines_removed": 0},
        })
        return

    cmd = [phpcsfixer_bin, "fix", "--allow-risky=yes"]
    if phpcsfixer_config:
        cmd += ["--config", phpcsfixer_config]
    cmd.append(file)

    env = os.environ.copy()
    env.setdefault("PHP_CS_FIXER_IGNORE_ENV", "1")

    start = time.time()
    try:
        # php-cs-fixer exits 0 when no fixes, 1 when fixes applied, 16+ on error
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
    except subprocess.TimeoutExpired:
        emit({
            "tool": "php-cs-fixer", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": "timeout after 60s"}],
            "duration_ms": 60000,
            "metrics": {"lines_added": 0, "lines_removed": 0},
        })
        return
    except (FileNotFoundError, OSError) as e:
        dur = int((time.time() - start) * 1000)
        emit({
            "tool": "php-cs-fixer", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": str(e)}],
            "duration_ms": dur,
            "metrics": {"lines_added": 0, "lines_removed": 0},
        })
        return

    dur = int((time.time() - start) * 1000)

    # php-cs-fixer: exit 0 = no changes, exit 1 = fixes applied, exit >=16 = error
    if r.returncode >= 16:
        msg = (r.stderr.strip() or r.stdout.strip())[:500]
        emit({
            "tool": "php-cs-fixer", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "php-cs-fixer", "msg": msg}],
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
        "tool": "php-cs-fixer",
        "file": file,
        "ok": True,
        "count": 0,
        "errors": [],
        "duration_ms": dur,
        "metrics": {"lines_added": added, "lines_removed": removed},
    })


if __name__ == "__main__":
    main()
