#!/usr/bin/env python3










from __future__ import annotations

import json
import os
import subprocess
import sys
import pathlib
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "common"))
from source_context import context_fields
from refusal import guard_main, is_refusal, skipped
from linebreaks import split_lines
from spawnable import argv0, spawnable


SKIP_PATTERNS_ENV = "PHPSTAN_SKIP_PATTERNS"











COUNT_CONTRACT = {"count_basis": "measured", "errors_truncated": False}


def first_line(text: str) -> str:

    for line in split_lines(text or ""):
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def refusal_line(text: str) -> str:






    for line in split_lines(text or ""):
        stripped = line.strip()
        if stripped and is_refusal(stripped, SKIP_PATTERNS_ENV):
            return stripped
    return first_line(text)


def report_lines(stdout: str) -> list:





























    rest = []
    for line in split_lines(stdout or ""):
        stripped = line.strip()
        if stripped and not is_refusal(stripped, SKIP_PATTERNS_ENV):
            rest.append(stripped)
    return rest


def unreadable_report(lines: list, limit: int = 200) -> str:






    body = " ".join(" ".join(lines).split())
    if not body:
        return "phpstan output not json"
    if len(body) > limit:
        body = body[:limit] + f"... (+{len(body) - limit} chars)"
    return f"phpstan output not json: {body}"


def emit(obj: dict) -> None:
    print(json.dumps(obj))


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({
            "tool": "phpstan", "file": "", "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": "no file arg"}],
            "duration_ms": 0,
        })
        return

    file = sys.argv[1]

    phpstan_bin = os.environ.get("PHPSTAN_BIN", "phpstan")
    phpstan_memory = os.environ.get("PHPSTAN_MEMORY", "1G")
    phpstan_config = os.environ.get("PHPSTAN_CONFIG", "")
    phpstan_level = os.environ.get("PHPSTAN_LEVEL", "")














    if not spawnable(phpstan_bin):
        emit({
            "tool": "phpstan", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": f"PHPSTAN_BIN not found: {phpstan_bin}"}],
            "duration_ms": 0,
        })
        return










    if not spawnable("php"):
        emit({
            "tool": "phpstan", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": "php not found"}],
            "duration_ms": 0,
        })
        return

    cmd = [argv0("php"), "-d", f"memory_limit={phpstan_memory}", argv0(phpstan_bin), "analyse"]
    if phpstan_config:
        cmd += ["-c", phpstan_config]
    if phpstan_level:
        cmd += ["--level", phpstan_level]
    cmd += ["--no-progress", "--error-format=json", "--", file]

    start = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        dur = int((time.time() - start) * 1000)
        emit({
            "tool": "phpstan", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": "php binary not found"}],
            "duration_ms": dur,
        })
        return
    except subprocess.TimeoutExpired:
        emit({
            "tool": "phpstan", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": "timeout"}],
            "duration_ms": int((time.time() - start) * 1000),
        })
        return
    dur = int((time.time() - start) * 1000)

    raw = r.stdout.strip()
    try:
        data = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        data = None

    if data is None:







        combined = os.linesep.join([r.stdout or "", r.stderr or ""])





        unread = report_lines(r.stdout or "")
        if not unread and is_refusal(combined, SKIP_PATTERNS_ENV):
            reason = refusal_line(combined) or "phpstan declined to analyse"
            emit(skipped("phpstan", file, reason, dur))
            return
        if raw:
            emit({
                "tool": "phpstan", "file": file, "ok": False, "count": 1,
                "errors": [{"line": None, "col": None, "severity": "error",
                            "code": "adapter",
                            "msg": unreadable_report(unread)}],
                "duration_ms": dur,
            })
            return
        if r.returncode != 0:
            detail = first_line(r.stderr) or "no output"
            emit({
                "tool": "phpstan", "file": file, "ok": False, "count": 1,
                "errors": [{"line": None, "col": None, "severity": "error",
                            "code": "adapter",
                            "msg": f"phpstan produced no result (exit {r.returncode}): {detail}"}],
                "duration_ms": dur,
            })
            return









        emit(skipped("phpstan", file, "phpstan exited 0 and produced no output",
                     dur))
        return

    count = int(data.get("totals", {}).get("file_errors", 0))
    errors = []
    for fdata in data.get("files", {}).values():
        for m in fdata.get("messages", []):
            line = m.get("line")
            errors.append({
                "line": line,
                "col": None,
                "severity": "error",
                "code": m.get("identifier"),
                "msg": m.get("message", ""),
                **context_fields(file, line),
            })

    emit({
        "tool": "phpstan",
        "file": file,
        "ok": count == 0,
        "count": count,
        "errors": errors,
        "duration_ms": dur,
        **COUNT_CONTRACT,
    })


if __name__ == "__main__":
    guard_main("phpstan", main)
