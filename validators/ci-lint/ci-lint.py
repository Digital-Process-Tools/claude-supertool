#!/usr/bin/env python3









































from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "common"))
from refusal import absent, guard_main
from bin_resolve import describe_unresolved, resolve_bin_cmd
from spawnable import already_a_path, spawnable

TOOL = "ci-lint"















_VALID_MARKER = "is valid"


def _invalid_marker_for(file: str) -> str:
    return f"{os.path.basename(file).lower()} is invalid."






COUNT_CONTRACT = {"count_basis": "measured", "errors_truncated": False}


def emit(obj: dict) -> None:
    print(json.dumps(obj))


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": TOOL, "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0, **COUNT_CONTRACT})
        return

    file = sys.argv[1]
    glab_bin_cmd_str = os.environ.get("GLAB_BIN", "glab")







    bin_cmd = resolve_bin_cmd(glab_bin_cmd_str, "glab")
    glab_bin = bin_cmd[0]

    if not spawnable(glab_bin) and not already_a_path(glab_bin):
        emit(absent(TOOL, file, f"GLAB_BIN not found: {describe_unresolved(glab_bin_cmd_str, glab_bin)}", 0))
        return

    if not os.path.isfile(file):
        emit({"tool": TOOL, "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "file not found"}],
              "duration_ms": 0, **COUNT_CONTRACT})
        return

    start = time.time()
    try:
        r = subprocess.run([*bin_cmd, "ci", "lint", "--", file], capture_output=True,
                            text=True, timeout=30, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:


        emit({"tool": TOOL, "file": file,
              "duration_ms": int((time.time() - start) * 1000),
              "skipped": "glab ci lint timed out after 30s -- could not "
                         "confirm GitLab would accept this config"})
        return
    except OSError as e:
        emit(absent(TOOL, file, str(e), int((time.time() - start) * 1000)))
        return

    dur = int((time.time() - start) * 1000)
    combined = (r.stdout + "\n" + r.stderr).strip()
    low = combined.lower()

    if r.returncode == 0 and _VALID_MARKER in low:
        emit({"tool": TOOL, "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": dur, **COUNT_CONTRACT})
        return

    if _invalid_marker_for(file) in low:
        msg = combined.strip()[:500]
        emit({"tool": TOOL, "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": TOOL, "msg": msg}],
              "duration_ms": dur, **COUNT_CONTRACT})
        return





    reason = combined.strip()[:500] or f"glab ci lint exited {r.returncode} " \
        "with no output"
    emit({"tool": TOOL, "file": file, "duration_ms": dur,
          "skipped": f"could not confirm this config with GitLab: {reason}"})


if __name__ == "__main__":
    guard_main(TOOL, main)
