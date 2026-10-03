#!/usr/bin/env python3





















































from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "common"))
from source_context import context_fields
from refusal import guard_main, required, required_but_absent, skipped, tool_fault
from npx_absent import is_npx_absent
from spawnable import argv0, spawnable

TOOL = "eslint"


def contained_target(file: str) -> str:












    if not file or os.path.isabs(file) or not file.startswith("-"):
        return file
    return os.path.join(os.curdir, file)

TIMEOUT_S = 60

RC_CLEAN = 0
RC_FINDINGS = 1

INSTALL_HINT = ("eslint not found (neither on PATH nor resolvable through "
                "`npx --no-install`) — `npm install --save-dev eslint`")





_NO_CONFIG = (
    "couldn't find an eslint.config",
    "couldn't find a configuration file",
    "no eslint configuration found",
)



_IGNORED = "file ignored"


def emit(d: dict) -> None:
    print(json.dumps(d))


def _adapter_error(file: str, msg: str, dur_ms: int) -> None:
    emit({"tool": TOOL, "file": file, "ok": False, "count": 1,
          "errors": [{"line": None, "col": None, "severity": "error",
                      "code": "adapter", "msg": msg}],
          "duration_ms": dur_ms})


def _decline(file: str, reason: str, dur_ms: int) -> None:

    if required(TOOL):
        _adapter_error(file, required_but_absent(TOOL, reason), dur_ms)
    else:
        emit(skipped(TOOL, file, reason, dur_ms))


def _resolve_cmd() -> list:

    if spawnable(TOOL):
        return [argv0(TOOL)]
    if spawnable("npx"):


        return [argv0("npx"), "--no-install", TOOL]
    return []


def _ignored_reason(messages: list) -> str | None:





    if not messages:
        return None
    for m in messages:
        if not isinstance(m, dict):
            return None
        if m.get("ruleId") is not None or m.get("fatal"):
            return None
        if _IGNORED not in (m.get("message") or "").lower():
            return None
    text = " ".join((m.get("message") or "") for m in messages)
    return ("eslint declined to lint this file — it matched an ignore "
            f"pattern: {text.strip()[:200]}")


def _severity(msg: dict) -> str:
    if msg.get("fatal") or msg.get("severity") == 2:
        return "error"
    return "warning"


def _to_error(msg: dict, file: str) -> dict:
    line = msg.get("line")
    text = (msg.get("message") or "").strip().replace("\n", " ")[:300]
    err = {
        "line": line,
        "col": msg.get("column"),
        "severity": _severity(msg),
        "code": msg.get("ruleId"),
        "msg": text,
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

    base = _resolve_cmd()
    if not base:
        _decline(file, INSTALL_HINT, int((time.time() - start) * 1000))
        return
    via_npx = base[0] != TOOL

    try:
        r = subprocess.run(base + ["-f", "json", contained_target(file)], capture_output=True,
                           text=True, timeout=TIMEOUT_S, encoding="utf-8",
                           errors="replace")
    except FileNotFoundError:
        _decline(file, "eslint resolved but could not be executed",
                 int((time.time() - start) * 1000))
        return
    except subprocess.TimeoutExpired:
        _adapter_error(file, f"timeout — eslint did not return within "
                             f"{TIMEOUT_S}s; the file was NOT checked",
                       int((time.time() - start) * 1000))
        return

    dur = int((time.time() - start) * 1000)
    body = (r.stdout or "").strip()
    stderr = (r.stderr or "").strip()

    if not body:
        lowered = stderr.lower()
        if via_npx and is_npx_absent(lowered, TOOL):



            _decline(file, INSTALL_HINT, dur)
            return
        if any(p in lowered for p in _NO_CONFIG):
            _decline(file,
                     "eslint found no resolvable configuration "
                     "(eslint.config.js) — this file was not linted, and no "
                     "fallback ruleset is invented for it", dur)
            return
        _adapter_error(file, tool_fault("eslint", r.returncode,
                                        stderr or "(no output)"), dur)
        return

    try:
        results = json.loads(body)
    except ValueError:
        _adapter_error(file, tool_fault("eslint", r.returncode,
                                        r.stdout or stderr or ""), dur)
        return

    if not isinstance(results, list):
        _adapter_error(file, tool_fault("eslint", r.returncode,
                                        f"expected a JSON array, got "
                                        f"{type(results).__name__}"), dur)
        return

    messages = []
    for res in results:
        if isinstance(res, dict) and isinstance(res.get("messages"), list):
            messages.extend(m for m in res["messages"] if isinstance(m, dict))

    ignored = _ignored_reason(messages)
    if ignored is not None:
        _decline(file, ignored, dur)
        return

    errors = [_to_error(m, file) for m in messages]
    emit({"tool": TOOL, "file": file, "ok": not errors, "count": len(errors),
          "errors": errors, "duration_ms": dur})


if __name__ == "__main__":
    guard_main(TOOL, main)
