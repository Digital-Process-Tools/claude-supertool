#!/usr/bin/env python3
"""cargo-check validator adapter — Rust compilation check via `cargo check`.

NOTE: This validator compiles the entire crate — it can be slow (5-30s on first
run). Consider disabling it for hot-loop editing via `opt_in: true` in your
.supertool.json validator config.

Requires cargo (ships with Rust). If missing, exits 0 with a stderr warning.
Finds Cargo.toml by walking up from the .rs file. Skips if none found.
Usage:  cargo-check.py <file>
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "common"))
from source_context import source_context


def emit(d: dict) -> None:
    print(json.dumps(d))


def _find_crate_root(file: str) -> Path | None:
    """Walk parent dirs from file until Cargo.toml is found."""
    p = Path(file).resolve().parent
    while True:
        if (p / "Cargo.toml").exists():
            return p
        parent = p.parent
        if parent == p:
            return None
        p = parent


def _parse_errors(output: str) -> list[dict]:
    """Extract error lines from cargo --message-format=short output."""
    errors = []
    # Short format: "path/to/file.rs:LINE:COL: error[EXXXX]: message"
    pattern = re.compile(r"^(.+?):(\d+):(\d+):\s+(error|warning)\[?([^\]]*)\]?:\s+(.+)$")
    for line in output.splitlines():
        m = pattern.match(line)
        if m:
            severity = m.group(4)
            if severity != "error":
                continue
            src_file = m.group(1)
            ln = int(m.group(2))
            err = {
                "line": ln,
                "col": int(m.group(3)),
                "severity": "error",
                "code": m.group(5) or "compile",
                "msg": m.group(6).strip()[:300],
                "source_context": source_context(src_file, ln),
            }
            errors.append(err)
    return errors


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": "cargo-check", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return

    file = sys.argv[1]
    start = time.time()

    if not shutil.which("cargo"):
        print("cargo-check: cargo not found on PATH, skipping", file=sys.stderr)
        emit({"tool": "cargo-check", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": int((time.time() - start) * 1000)})
        return

    crate_root = _find_crate_root(file)
    if crate_root is None:
        print("cargo-check: no Cargo.toml found, skipping", file=sys.stderr)
        emit({"tool": "cargo-check", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": int((time.time() - start) * 1000)})
        return

    try:
        r = subprocess.run(
            ["cargo", "check", "--message-format=short", "--quiet"],
            capture_output=True, text=True, timeout=120,
            cwd=str(crate_root),
        )
    except FileNotFoundError:
        print("cargo-check: cargo not found on PATH, skipping", file=sys.stderr)
        emit({"tool": "cargo-check", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": int((time.time() - start) * 1000)})
        return
    except subprocess.TimeoutExpired:
        emit({"tool": "cargo-check", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "timeout (cargo check exceeded 120s)"}],
              "duration_ms": 120000})
        return

    dur = int((time.time() - start) * 1000)

    if r.returncode == 0:
        emit({"tool": "cargo-check", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": dur})
        return

    output = r.stderr or r.stdout or ""
    errors = _parse_errors(output)
    if not errors:
        errors = [{"line": None, "col": None, "severity": "error",
                   "code": "compile", "msg": output.strip()[:300] or "cargo check failed"}]

    emit({"tool": "cargo-check", "file": file, "ok": False, "count": len(errors),
          "errors": errors, "duration_ms": dur})


if __name__ == "__main__":
    main()
