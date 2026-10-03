#!/usr/bin/env python3









































































from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "common"))
from refusal import guard_main, required, required_but_absent, skipped, tool_fault
from spawnable import argv0, spawnable

TOOL = "gitleaks"

TIMEOUT_S = 60



RC_CLEAN = 0
RC_FINDINGS = 1

INSTALL_HINT = (
    "gitleaks not found on PATH — this file was NOT scanned for secrets "
    "(`brew install gitleaks`). A clean row here would have meant nothing")


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


def _to_error(item: dict) -> dict:






    rule = item.get("RuleID") or None
    desc = (item.get("Description") or "").strip().replace("\n", " ")[:200]
    line = item.get("StartLine")
    return {
        "line": line if isinstance(line, int) else None,
        "col": item.get("StartColumn") if isinstance(
            item.get("StartColumn"), int) else None,
        "severity": "error",
        "code": rule,
        "msg": (f"possible secret ({desc or rule or 'unnamed rule'}) — the "
                f"matched value is deliberately not printed. Detection is "
                f"pattern-based: rotate it if it is real, or annotate the "
                f"line with `gitleaks:allow` if it is a fixture"),
    }


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        _adapter_error("", "no file arg", 0)
        return

    file = sys.argv[1]
    start = time.time()

    if not spawnable(TOOL):
        _decline(file, INSTALL_HINT, int((time.time() - start) * 1000))
        return



    workdir = tempfile.mkdtemp(prefix="supertool-gitleaks-")
    os.chmod(workdir, 0o700)
    report = os.path.join(workdir, "report.json")
    try:
        cmd = [argv0(TOOL), "detect", "--no-git", "--redact", "--no-banner",
               "--source", file, "--report-format", "json",
               "--report-path", report]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=TIMEOUT_S, encoding="utf-8",
                               errors="replace")
        except FileNotFoundError:
            _decline(file, "gitleaks on PATH but could not be executed — this "
                           "file was NOT scanned for secrets",
                     int((time.time() - start) * 1000))
            return
        except subprocess.TimeoutExpired:
            _adapter_error(file, f"timeout — gitleaks did not return within "
                                 f"{TIMEOUT_S}s; the file was NOT scanned",
                           int((time.time() - start) * 1000))
            return

        dur = int((time.time() - start) * 1000)




        try:
            with open(report, encoding="utf-8") as fh:
                body = fh.read().strip()
        except OSError:
            body = ""

        if not body:
            if r.returncode == RC_CLEAN:


                emit({"tool": TOOL, "file": file, "ok": True, "count": 0,
                      "errors": [], "duration_ms": dur})
                return
            _adapter_error(file, tool_fault("gitleaks detect", r.returncode,
                                            r.stderr or r.stdout or ""), dur)
            return

        try:
            items = json.loads(body)
        except ValueError:


            _adapter_error(file, tool_fault(
                "gitleaks detect", r.returncode,
                "the JSON report could not be parsed (its contents are not "
                "quoted here, since a secret report is what it is)"), dur)
            return

        if not isinstance(items, list):
            _adapter_error(file, tool_fault(
                "gitleaks detect", r.returncode,
                f"expected a JSON array report, got {type(items).__name__}"),
                dur)
            return

        if r.returncode not in (RC_CLEAN, RC_FINDINGS) and not items:
            _adapter_error(file, tool_fault("gitleaks detect", r.returncode,
                                            r.stderr or r.stdout or ""), dur)
            return

        errors = [_to_error(i) for i in items if isinstance(i, dict)]
        emit({"tool": TOOL, "file": file, "ok": not errors,
              "count": len(errors), "errors": errors, "duration_ms": dur})
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    guard_main(TOOL, main)
