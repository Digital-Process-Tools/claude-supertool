#!/usr/bin/env python3















from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "common"))
from source_context import context_fields
from refusal import absent, guard_main, skipped, tool_fault
from npx_absent import is_npx_absent
from spawnable import argv0, spawnable

TOOL = "stylelint"
INSTALL_HINT = ("stylelint not found, globally or via npx — this file was NOT "
                "linted (`npm install -g stylelint`)")





IGNORED_MARKERS = ("allfilesignorederror", "input files were ignored")
IGNORED_REASON = ("stylelint declined to lint this file — every input it "
                  "resolved was excluded by an ignore pattern "
                  "(`.stylelintignore`, or `ignoreFiles` in the config), so "
                  "nothing here is a verdict about it")


def emit(d: dict) -> None:
    print(json.dumps(d))


def contained_target(file: str) -> str:












    if not file or os.path.isabs(file) or not file.startswith("-"):
        return file
    return os.path.join(os.curdir, file)


def _resolve_cmd() -> list:

    if spawnable("stylelint"):
        return [argv0("stylelint")]
    if spawnable("npx"):
        return [argv0("npx"), "--no-install", "stylelint"]
    return []


def _report(streams) -> "list | None":





    for text in streams:
        text = (text or "").strip()
        if not text:
            continue
        try:
            data = json.loads(text)
        except ValueError:
            continue
        if isinstance(data, list):
            return data
    return None


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": "stylelint", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return
    file = sys.argv[1]
    start = time.time()
    base = _resolve_cmd()
    via_npx = bool(base) and base[0] != "stylelint"
    if not base:



        emit(absent(TOOL, file, INSTALL_HINT,
                    int((time.time() - start) * 1000)))
        return
    try:
        r = subprocess.run(base + ["--formatter", "json", contained_target(file)],
                           capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")
    except FileNotFoundError:


        emit(absent(TOOL, file, "stylelint was found on PATH but could not be "
                                "executed — this file was NOT linted",
                    int((time.time() - start) * 1000)))
        return
    except subprocess.TimeoutExpired:
        emit({"tool": "stylelint", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "timeout"}],
              "duration_ms": int((time.time() - start) * 1000)})
        return
    dur = int((time.time() - start) * 1000)
    data = _report((r.stdout, r.stderr))
    if data is None:
        noise = ((r.stderr or "") + "\n" + (r.stdout or "")).strip()
        lowered = noise.lower()
        if via_npx and is_npx_absent(lowered, TOOL):





            emit(absent(TOOL, file, INSTALL_HINT, dur))
            return
        if any(m in lowered for m in IGNORED_MARKERS):
            emit(skipped(TOOL, file, IGNORED_REASON, dur))
            return



        emit({"tool": "stylelint", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter",
                          "msg": tool_fault("stylelint", r.returncode, noise)}],
              "duration_ms": dur})
        return
    errors = []
    for item in data:
        for w in item.get("warnings", []):
            ln = w.get("line")
            err = {
                "line": ln,
                "col": w.get("column"),
                "severity": w.get("severity", "warning"),
                "code": w.get("rule"),
                "msg": (w.get("text") or "")[:300],
            }
            if ln is not None:
                err.update(context_fields(file, ln))
            errors.append(err)
    emit({"tool": "stylelint", "file": file, "ok": len(errors) == 0,
          "count": len(errors), "errors": errors, "duration_ms": dur})


if __name__ == "__main__":
    guard_main(TOOL, main)
