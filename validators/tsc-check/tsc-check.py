#!/usr/bin/env python3





























from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "common"))
from source_context import context_fields
from pkg_paths import attribute
from refusal import absent, guard_main
from spawnable import argv0, spawnable
from linebreaks import split_lines

TOOL = "tsc-check"
INSTALL_HINT = ("tsc not found on PATH — this file was NOT type-checked "
                "(`npm install -g typescript`)")






TIMEOUT_S = 30












ANSI_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\[[0-?]*[ -/]*"
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)|\][^\x07\x1b]*"
    r"|[@-Z\\-_]|)"
)


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def contained_target(file: str) -> str:






















    if not file or os.path.isabs(file) or file[0] not in "@-":
        return file
    return os.path.join(os.curdir, file)


def _elsewhere(reported: str, ln: str, col: str, severity: str,
               code: str, msg: str) -> dict:







    return {"line": None, "col": None, "severity": severity, "code": "adapter",
            "msg": f"in {reported}({ln},{col}) (another file in this program): "
                   f"{code}: {msg}"}


def _unplaceable(reported: str, ln: str, col: str, severity: str,
                 code: str, msg: str) -> dict:






    return {"line": None, "col": None, "severity": severity, "code": "adapter",
            "msg": f"tsc reported {reported}({ln},{col}) — this adapter could "
                   f"not tell whether that is the file under validation: "
                   f"{code}: {msg}"}







DIAG_RE = re.compile(
    r"^(?P<path>.*?)\((?P<line>\d+),(?P<col>\d+)\):\s+"
    r"(?P<severity>\w+)\s+(?P<code>TS\d+):\s+(?P<msg>.+)$")


def parse_diagnostics(output: str, file: str, base: str) -> list:






    errors = []
    for line in split_lines(output):
        m = DIAG_RE.match(line)
        if not m:
            continue
        reported = m.group("path").strip()
        ln, col = m.group("line"), m.group("col")
        code, msg = m.group("code"), m.group("msg").strip()[:300]
        severity = (m.group("severity")
                    if m.group("severity") in ("error", "warning") else "error")
        where = attribute(reported, target=file, base=base)
        if where == "this":
            err = {"line": int(ln), "col": int(col), "severity": severity,
                   "code": code, "msg": msg}
            err.update(context_fields(file, int(ln)))
        elif where == "other":
            err = _elsewhere(reported, ln, col, severity, code, msg)
        else:
            err = _unplaceable(reported, ln, col, severity, code, msg)
        errors.append(err)
    return errors


def emit(d: dict) -> None:
    print(json.dumps(d))


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": "tsc-check", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return

    file = sys.argv[1]
    start = time.time()

    if not spawnable("tsc"):
        emit(absent(TOOL, file, INSTALL_HINT,
                    int((time.time() - start) * 1000)))
        return

    try:
        result = subprocess.run(
            [argv0("tsc"), "--noEmit", "--skipLibCheck", "--pretty", "false",
             contained_target(file)],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:



        emit(absent(TOOL, file, "tsc on PATH but could not be executed — "
                                "this file was NOT type-checked",
                    int((time.time() - start) * 1000)))
        return
    except subprocess.TimeoutExpired:







        emit({"tool": "tsc-check", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter",
                          "msg": f"timeout — tsc did not return within {TIMEOUT_S}s; "
                                 "the file was NOT checked"}],
              "duration_ms": int((time.time() - start) * 1000)})
        return

    duration = int((time.time() - start) * 1000)

    if result.returncode == 0:
        emit({"tool": "tsc-check", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": duration})
        return

    output = strip_ansi(result.stdout + result.stderr).strip()


    errors = parse_diagnostics(output, file, os.getcwd())















    if not errors:
        if output:
            said = ("its output could not be parsed: "
                    + " ".join(output.split())[:200])
        else:
            said = "said nothing at all"
        errors = [{"line": None, "col": None, "severity": "error",
                   "code": "adapter",
                   "msg": f"tsc exited {result.returncode} and {said} — this "
                          f"file was NOT type-checked"}]

    emit({"tool": "tsc-check", "file": file, "ok": False, "count": len(errors),
          "errors": errors, "duration_ms": duration})


if __name__ == "__main__":
    guard_main(TOOL, main)
