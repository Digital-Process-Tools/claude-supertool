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
            "tool": "phpcbf", "file": "", "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": "no file arg"}],
            "duration_ms": 0,
            "metrics": {"lines_added": 0, "lines_removed": 0,
                        "first_changed_line": None, "last_changed_line": None},
        })
        return

    file = sys.argv[1]
    start = time.time()
    phpcbf_bin_cmd_str = os.environ.get("PHPCBF_BIN", "phpcbf")








    bin_cmd = resolve_bin_cmd(phpcbf_bin_cmd_str, "phpcbf")
    phpcbf_bin = bin_cmd[0]
    phpcbf_standard = os.environ.get("PHPCBF_STANDARD", "PSR12")

    if not spawnable(phpcbf_bin) and not already_a_path(phpcbf_bin):
        emit({
            "tool": "phpcbf", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter",
                        "msg": f"PHPCBF_BIN not found: {describe_unresolved(phpcbf_bin_cmd_str, phpcbf_bin)}"}],
            "duration_ms": 0,
            "metrics": {"lines_added": 0, "lines_removed": 0,
                        "first_changed_line": None, "last_changed_line": None},
        })
        return

    try:
        before = open(file, encoding="utf-8", errors="replace").read()
    except OSError as e:
        emit({
            "tool": "phpcbf", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": f"cannot read file: {e}"}],
            "duration_ms": int((time.time() - start) * 1000),
            "metrics": {"lines_added": 0, "lines_removed": 0,
                        "first_changed_line": None, "last_changed_line": None},
        })
        return

    cmd = [*bin_cmd, f"--standard={phpcbf_standard}", file]

    try:

        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        emit({
            "tool": "phpcbf", "file": file, "ok": False, "count": 1,
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
            "tool": "phpcbf", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": str(e)}],
            "duration_ms": dur,
            "metrics": {"lines_added": 0, "lines_removed": 0,
                        "first_changed_line": None, "last_changed_line": None},
        })
        return

    dur = int((time.time() - start) * 1000)





    if r.returncode >= 3:
        msg = (r.stderr.strip() or r.stdout.strip())[:500]
        emit({
            "tool": "phpcbf", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "phpcbf", "msg": msg}],
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
        "tool": "phpcbf",
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
    guard_main("phpcbf", main)
