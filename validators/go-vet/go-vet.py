#!/usr/bin/env python3


































from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "common"))
from source_context import context_fields
from pkg_paths import attribute
from refusal import absent, guard_main, skipped, tool_fault
from spawnable import argv0, spawnable
from linebreaks import split_lines

TOOL = "go-vet"
BINARY = "go"
INSTALL_HINT = ("go not found on PATH — this package was NOT vetted "
                "(install the Go toolchain from https://go.dev/dl/)")
TIMEOUT = 60



MAX_ERRORS = 50












DIAG = re.compile(
    r"^(?P<load>vet(?:\.exe)?:\s+)?(?P<path>.+?):(?P<line>\d+):(?P<col>\d+):"
    r"\s*(?P<msg>.*)$")


def emit(d: dict) -> None:
    print(json.dumps(d))


def _adapter_error(file: str, msg: str, dur_ms: int) -> dict:
    return {"tool": TOOL, "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": msg}],
            "duration_ms": dur_ms}


def _module_root(start: pathlib.Path) -> pathlib.Path | None:

    for d in [start, *start.parents]:
        if (d / "go.mod").is_file():
            return d
    return None


def _elsewhere(reported: str, line: str, col: str, msg: str) -> dict:






    return {"line": None, "col": None, "severity": "warning", "code": "adapter",
            "msg": f"in {reported}:{line}:{col} (another file in this package): {msg}"}


def _unplaceable(reported: str, line: str, col: str, msg: str) -> dict:






    return {"line": None, "col": None, "severity": "warning", "code": "adapter",
            "msg": f"go vet reported {reported}:{line}:{col} — this adapter could "
                   f"not tell whether that is the file under validation: {msg}"}


def _os_reason(exc: OSError) -> str:







    return exc.strerror or str(exc) or "the OS reported no reason"


def _truncation_notice(hidden: int) -> dict:







    return {"line": None, "col": None, "severity": "warning", "code": "adapter",
            "msg": f"{hidden} further go vet finding(s) in this package are not "
                   f"shown — `count` reports the full number"}


def _parse(output: str, target: str, base: str) -> list:
    errors = []
    for raw in split_lines(output):
        text = raw.strip()
        if not text or text.startswith("#"):
            continue  
        m = DIAG.match(text)
        if not m:
            continue
        reported, line, col, msg = (m.group("path"), m.group("line"),
                                    m.group("col"), m.group("msg"))




        severity = "error" if m.group("load") else "warning"
        where = attribute(reported, target=target, base=base)
        if where == "this":
            err = {"line": int(line), "col": int(col), "severity": severity,
                   "code": "load" if severity == "error" else None,
                   "msg": msg}
            err.update(context_fields(target, int(line)))
        elif where == "other":
            err = _elsewhere(reported, line, col, msg)
            err["severity"] = severity
        else:
            err = _unplaceable(reported, line, col, msg)
            err["severity"] = severity




        errors.append((0 if where == "this" else 1, err))
    errors.sort(key=lambda pair: pair[0])
    return [err for _, err in errors]


def main() -> None:
    start = time.time()

    def ms() -> int:
        return int((time.time() - start) * 1000)

    if len(sys.argv) < 2 or not sys.argv[1]:
        emit(_adapter_error("", "no file arg", ms()))
        return

    file = sys.argv[1]

    if not os.path.isfile(file):
        emit(_adapter_error(file, "file not found", ms()))
        return

    if not spawnable(BINARY):
        emit(absent(TOOL, file, INSTALL_HINT, ms()))
        return

    target = os.path.abspath(file)
    pkg_dir = os.path.dirname(target)

    if _module_root(pathlib.Path(pkg_dir)) is None:
        emit(skipped(TOOL, file,
                     "no go.mod at or above this file — `go vet` will not load a "
                     "package outside a module, so this file was NOT vetted",
                     ms()))
        return

    try:
        proc = subprocess.run([argv0(BINARY), "vet", "."], capture_output=True,
                              text=True, timeout=TIMEOUT, cwd=pkg_dir,
                              encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:



        emit(_adapter_error(file, f"go vet timed out after {TIMEOUT}s", ms()))
        return
    except OSError as exc:



        emit(_adapter_error(
            file,
            f"go vet could not be run: {exc.__class__.__name__} — {_os_reason(exc)}",
            ms()))
        return

    output = (proc.stderr or "") + "\n" + (proc.stdout or "")

    if proc.returncode == 0:
        emit({"tool": TOOL, "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": ms()})
        return



    if "go.mod file not found" in output or "cannot find main module" in output:
        first = (split_lines(output.strip()) or [""])[0][:200]
        emit(skipped(TOOL, file,
                     "go declined to load a module for this file, so it was NOT "
                     "vetted: " + first, ms()))
        return

    errors = _parse(output, target=target, base=pkg_dir)

    if not errors:



        emit(_adapter_error(file, tool_fault(TOOL, proc.returncode, output), ms()))
        return

    published = errors
    if len(errors) > MAX_ERRORS:
        published = errors[:MAX_ERRORS - 1]
        published.append(_truncation_notice(len(errors) - len(published)))

    emit({"tool": TOOL, "file": file, "ok": False, "count": len(errors),
          "errors": published, "duration_ms": ms()})


if __name__ == "__main__":
    guard_main(TOOL, main)
