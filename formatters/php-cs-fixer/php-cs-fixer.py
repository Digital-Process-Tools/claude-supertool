#!/usr/bin/env python3











from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent
                       / "validators" / "common"))
from refusal import guard_main  
from bin_resolve import describe_unresolved, resolve_bin_cmd  
from spawnable import already_a_path, spawnable  
from line_diff import line_diff as _line_diff  


def emit(obj: dict) -> None:
    print(json.dumps(obj))


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({
            "tool": "php-cs-fixer", "file": "", "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": "no file arg"}],
            "duration_ms": 0,
            "metrics": {"lines_added": 0, "lines_removed": 0,
                        "first_changed_line": None, "last_changed_line": None},
        })
        return

    file = sys.argv[1]
    start = time.time()
    phpcsfixer_bin_cmd_str = os.environ.get("PHPCSFIXER_BIN", "php-cs-fixer")








    bin_cmd = resolve_bin_cmd(phpcsfixer_bin_cmd_str, "php-cs-fixer")
    phpcsfixer_bin = bin_cmd[0]
    phpcsfixer_config = os.environ.get("PHPCSFIXER_CONFIG", "")

    if not spawnable(phpcsfixer_bin) and not already_a_path(phpcsfixer_bin):
        emit({
            "tool": "php-cs-fixer", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter",
                        "msg": f"PHPCSFIXER_BIN not found: {describe_unresolved(phpcsfixer_bin_cmd_str, phpcsfixer_bin)}"}],
            "duration_ms": 0,
            "metrics": {"lines_added": 0, "lines_removed": 0,
                        "first_changed_line": None, "last_changed_line": None},
        })
        return

    try:
        before = open(file, encoding="utf-8", errors="replace").read()
    except OSError as e:
        emit({
            "tool": "php-cs-fixer", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": f"cannot read file: {e}"}],
            "duration_ms": int((time.time() - start) * 1000),
            "metrics": {"lines_added": 0, "lines_removed": 0,
                        "first_changed_line": None, "last_changed_line": None},
        })
        return

    cmd = [*bin_cmd, "fix", "--allow-risky=yes"]
    if phpcsfixer_config:
        cmd += ["--config", phpcsfixer_config]
    cmd.append(file)

    env = os.environ.copy()

    try:

        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        emit({
            "tool": "php-cs-fixer", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": "timeout after 60s"}],
            "duration_ms": int((time.time() - start) * 1000),
            "metrics": {"lines_added": 0, "lines_removed": 0,
                        "first_changed_line": None, "last_changed_line": None},
        })
        return
    except (FileNotFoundError, OSError) as e:
        dur = int((time.time() - start) * 1000)
        emit({
            "tool": "php-cs-fixer", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": str(e)}],
            "duration_ms": dur,
            "metrics": {"lines_added": 0, "lines_removed": 0,
                        "first_changed_line": None, "last_changed_line": None},
        })
        return

    dur = int((time.time() - start) * 1000)


    if r.returncode >= 16:
        msg = (r.stderr.strip() or r.stdout.strip())[:500]
        emit({
            "tool": "php-cs-fixer", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "php-cs-fixer", "msg": msg}],
            "duration_ms": dur,
            "metrics": {"lines_added": 0, "lines_removed": 0,
                        "first_changed_line": None, "last_changed_line": None},
        })
        return

    verify_failed = None
    try:
        after = open(file, encoding="utf-8", errors="replace").read()
    except OSError as e:





        after = before
        verify_failed = f"could not re-read file to verify changes: {e}"

    added, removed, (first, last) = _line_diff(before, after)

    payload = {
        "tool": "php-cs-fixer",
        "file": file,
        "ok": True,
        "count": 0,
        "errors": [],
        "duration_ms": dur,
        "metrics": {"lines_added": added, "lines_removed": removed,
                    "first_changed_line": first, "last_changed_line": last},
    }
    if verify_failed:
        payload["verify_failed"] = verify_failed
    emit(payload)


if __name__ == "__main__":
    guard_main("php-cs-fixer", main)
