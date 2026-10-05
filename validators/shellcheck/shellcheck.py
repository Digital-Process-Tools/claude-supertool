#!/usr/bin/env python3









































from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "common"))
from source_context import context_fields
from refusal import guard_main, required, required_but_absent, skipped, tool_fault
from spawnable import argv0, spawnable

TOOL = "shellcheck"




TIMEOUT_S = 30




RC_CLEAN = 0
RC_FINDINGS = 1

INSTALL_HINT = ("shellcheck not found on PATH — `brew install shellcheck` / "
                "`apt install shellcheck`")





_CANNOT_CLASSIFY = "shellcheck can't be used with"
_NO_SHEBANG = "tell what kind of shell"


def emit(d: dict) -> None:
    print(json.dumps(d))


def _adapter_error(file: str, msg: str, dur_ms: int) -> None:
    emit({"tool": TOOL, "file": file, "ok": False, "count": 1,
          "errors": [{"line": None, "col": None, "severity": "error",
                      "code": "adapter", "msg": msg}],
          "duration_ms": dur_ms})


def _severity(level: object) -> str:








    text = str(level or "").lower()
    if text == "error":
        return "error"
    if text in ("info", "style"):
        return "info"
    return "warning"


def _to_error(item: dict, file: str) -> dict:
    line = item.get("line")
    code = item.get("code")
    err = {
        "line": line,
        "col": item.get("column"),
        "severity": _severity(item.get("level")),
        "code": f"SC{code}" if code is not None else None,
        "msg": (item.get("message") or "").strip().replace("\n", " ")[:300],
    }
    if isinstance(line, int):
        err.update(context_fields(file, line))
    return err


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        _adapter_error("", "no file arg", 0)
        return

    file = sys.argv[1]
    start = time.time()

    if not spawnable(TOOL):
        dur = int((time.time() - start) * 1000)
        if required(TOOL):
            _adapter_error(file, required_but_absent(TOOL, INSTALL_HINT), dur)
        else:
            emit(skipped(TOOL, file, INSTALL_HINT, dur))
        return

    try:
        r = subprocess.run([argv0(TOOL), "-f", "json", "--", file], capture_output=True,
                           text=True, timeout=TIMEOUT_S, encoding="utf-8",
                           errors="replace")
    except FileNotFoundError:

        dur = int((time.time() - start) * 1000)
        reason = "shellcheck on PATH but could not be executed"
        if required(TOOL):
            _adapter_error(file, required_but_absent(TOOL, reason), dur)
        else:
            emit(skipped(TOOL, file, reason, dur))
        return
    except subprocess.TimeoutExpired:


        _adapter_error(file, f"timeout — shellcheck did not return within "
                             f"{TIMEOUT_S}s; the file was NOT checked",
                       int((time.time() - start) * 1000))
        return

    dur = int((time.time() - start) * 1000)
    body = (r.stdout or "").strip()
    stderr = (r.stderr or "").strip()




    if r.returncode not in (RC_CLEAN, RC_FINDINGS):
        lowered = stderr.lower()
        if _CANNOT_CLASSIFY in lowered or _NO_SHEBANG in lowered:
            emit(skipped(TOOL, file,
                         "shellcheck could not determine the shell dialect "
                         "(no shebang, no --shell) — this file was not checked",
                         dur))
            return
        _adapter_error(file, tool_fault("shellcheck", r.returncode,
                                        stderr or r.stdout or ""), dur)
        return

    try:
        items = json.loads(body) if body else []
    except ValueError:
        _adapter_error(file, tool_fault("shellcheck", r.returncode,
                                        r.stdout or stderr or ""), dur)
        return

    if not isinstance(items, list):
        _adapter_error(file, tool_fault("shellcheck", r.returncode,
                                        f"expected a JSON array, got "
                                        f"{type(items).__name__}"), dur)
        return

    errors = [_to_error(i, file) for i in items if isinstance(i, dict)]
    emit({"tool": TOOL, "file": file, "ok": not errors, "count": len(errors),
          "errors": errors, "duration_ms": dur})


if __name__ == "__main__":
    guard_main(TOOL, main)
