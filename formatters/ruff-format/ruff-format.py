#!/usr/bin/env python3
"""ruff format adapter. Emits SCHEMA.md JSON.

Runs `ruff format` on the target file and computes before/after line diff to
populate metrics.lines_added / lines_removed -- #2085.

`ruff format` rather than `black`: this repository's own CI lint leg already
depends on ruff (see .github/workflows/tests.yml), so the toolchain this
adapter dispatches to is already installed wherever the tests run, and the
absent-tool arm below rarely fires. A repo that prefers black can still wire
it in directly (see docs/formatters.md, "Adding your own") -- this adapter
only closes the gap that no *shipped* Python formatter existed at all.

Usage: ruff-format.py <file>

Env vars:
  RUFF_BIN     ruff binary (default: ruff)
  RUFF_CONFIG  --config path (optional)
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
            "tool": "ruff-format", "file": "", "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": "no file arg"}],
            "duration_ms": 0,
            "metrics": {"lines_added": 0, "lines_removed": 0},
        })
        return

    file = sys.argv[1]
    ruff_bin_cmd_str = os.environ.get("RUFF_BIN", "ruff")
    # Accept either a single binary path or a shlex-split command line.
    # Cross-platform test stubs pass e.g. "python /path/stub.py" so the
    # stub runs on Windows too (no #!/usr/bin/env bash dependency).
    import shlex as _shlex
    bin_cmd = _shlex.split(ruff_bin_cmd_str.replace("\\", "/"), posix=True) or ["ruff"]
    ruff_bin = bin_cmd[0]
    ruff_config = os.environ.get("RUFF_CONFIG", "")

    if not shutil.which(ruff_bin) and not (
        os.path.isfile(ruff_bin) and os.access(ruff_bin, os.X_OK)
    ):
        emit({
            "tool": "ruff-format", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter",
                        "msg": f"RUFF_BIN not found: {ruff_bin}"}],
            "duration_ms": 0,
            "metrics": {"lines_added": 0, "lines_removed": 0},
        })
        return

    try:
        before = open(file, encoding="utf-8", errors="replace").read()
    except OSError as e:
        emit({
            "tool": "ruff-format", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": f"cannot read file: {e}"}],
            "duration_ms": 0,
            "metrics": {"lines_added": 0, "lines_removed": 0},
        })
        return

    cmd = [*bin_cmd, "format"]
    if ruff_config:
        cmd += ["--config", ruff_config]
    cmd.append(file)

    start = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        emit({
            "tool": "ruff-format", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": "timeout after 30s"}],
            "duration_ms": 30000,
            "metrics": {"lines_added": 0, "lines_removed": 0},
        })
        return
    except (FileNotFoundError, OSError) as e:
        dur = int((time.time() - start) * 1000)
        emit({
            "tool": "ruff-format", "file": file, "ok": False, "count": 1,
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
            "tool": "ruff-format", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "ruff-format", "msg": msg}],
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
        "tool": "ruff-format",
        "file": file,
        "ok": True,
        "count": 0,
        "errors": [],
        "duration_ms": dur,
        "metrics": {"lines_added": added, "lines_removed": removed},
    })


if __name__ == "__main__":
    main()
