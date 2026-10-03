#!/usr/bin/env python3


















from __future__ import annotations

import json
import ntpath
import os
import posixpath
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "common"))
from source_context import context_fields
from refusal import absent, guard_main, skipped, tool_fault
from spawnable import argv0, spawnable
from linebreaks import split_lines

TOOL = "cargo-check"
INSTALL_HINT = ("cargo not found on PATH — this file was NOT compiled "
                "(install the Rust toolchain via rustup)")















COUNT_CONTRACT = {"count_basis": "total", "errors_truncated": False}


def emit(d: dict) -> None:
    print(json.dumps(d))


def _find_crate_root(file: str) -> Path | None:

    p = Path(file).resolve().parent
    while True:
        if (p / "Cargo.toml").exists():
            return p
        parent = p.parent
        if parent == p:
            return None
        p = parent


def _canon(path: object, normcase: Callable[[str], str] | None = None) -> str:






















    fold = normcase or os.path.normcase
    return fold(str(path)).replace("\\", "/")


def _is_abs(path: str) -> bool:









    return ntpath.isabs(path) or posixpath.isabs(path)


def _workspace_root(crate_root: Path,
                    run: Callable[..., object] | None = None) -> tuple[str | None, str]:
















    runner = run or subprocess.run
    try:
        r = runner([argv0("cargo"), "metadata", "--no-deps", "--format-version", "1"],
                   capture_output=True, text=True, timeout=30,
                   cwd=str(crate_root), encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return None, "cargo metadata timed out after 30s"
    except OSError as exc:
        return None, f"cargo metadata could not be run ({type(exc).__name__})"

    if getattr(r, "returncode", 1) != 0:
        return None, f"cargo metadata exited {r.returncode}"
    try:
        root = json.loads(r.stdout or "")["workspace_root"]
    except (ValueError, TypeError, KeyError):
        return None, "cargo metadata output was unreadable"
    if not isinstance(root, str) or not root.strip():
        return None, "cargo metadata output was unreadable"
    return root, ""


def _attribute(src_file: str, target: Path, target_raw: str = "",
               ws_root: str | None = None,
               normcase: Callable[[str], str] | None = None) -> str:





































    src = (src_file or "").strip()
    if not src:
        return "unknown"

    canon = posixpath.normpath(_canon(src, normcase))
    if _is_abs(src):
        src_forms = {canon}
        if os.path.isabs(src):
            try:
                src_forms.add(_canon(Path(src).resolve(), normcase))
            except OSError:
                pass
    else:
        if not (ws_root or "").strip():
            return "unknown"
        base = posixpath.normpath(_canon(ws_root, normcase))
        src_forms = {posixpath.normpath(posixpath.join(base, canon))}

    if src_forms & _target_forms(target, target_raw, normcase):
        return "this"
    return "other"


def _same_file(src_file: str, target: Path, target_raw: str = "",
               normcase: Callable[[str], str] | None = None,
               ws_root: str | None = None) -> bool:







    return _attribute(src_file, target, target_raw, ws_root, normcase) == "this"


def _target_forms(target: Path | str, target_raw: str = "",
                  normcase: Callable[[str], str] | None = None) -> set:




























    forms = set()
    for raw in (target, target_raw):
        text = str(raw or "").strip()
        if not text:
            continue
        if not _is_abs(text):
            text = os.path.abspath(text)
        forms.add(posixpath.normpath(_canon(text, normcase)))
    return forms


def _elsewhere_in_crate(src_file: str, ln: int, col: int, code: str, msg: str) -> str:









    label = f"error[{code}]" if code and code != "compile" else "error"
    return (f"cargo check reported {label} at {src_file}:{ln}:{col}, which is "
            f"not this file - the crate does not compile, so no verdict was "
            f"produced about this file: {msg}")


def _unplaceable(src_file: str, ln: int, col: int, code: str, msg: str,
                 reason: str) -> str:









    label = f"error[{code}]" if code and code != "compile" else "error"
    return (f"cargo check reported {label} at {src_file}:{ln}:{col}, a path "
            f"relative to the workspace root, and the workspace root could not "
            f"be read ({reason}) - so which file it names is unknown and no "
            f"verdict was produced about this file: {msg}")


def _parse_errors(output: str, target_file: str, ws_root: str | None = None,
                  ws_reason: str = "") -> list[dict]:

















    errors = []
    target = Path(target_file).resolve()

    pattern = re.compile(r"^(.+?):(\d+):(\d+):\s+(error|warning)\[?([^\]]*)\]?:\s+(.+)$")
    for line in split_lines(output):
        m = pattern.match(line)
        if m:
            severity = m.group(4)
            if severity != "error":
                continue
            src_file = m.group(1)
            ln = int(m.group(2))
            col = int(m.group(3))
            code = m.group(5) or "compile"
            msg = m.group(6).strip()[:300]
            verdict = _attribute(src_file, target, target_file, ws_root)
            if verdict == "this":






                errors.append({
                    "line": ln,
                    "col": col,
                    "severity": "error",
                    "code": code,
                    "msg": msg,
                    **context_fields(str(target), ln),
                })
            else:
                where = (_elsewhere_in_crate(src_file, ln, col, code, msg)
                         if verdict == "other"
                         else _unplaceable(src_file, ln, col, code, msg,
                                           ws_reason or "reason not recorded"))
                errors.append({
                    "line": None,
                    "col": None,
                    "severity": "error",
                    "code": "adapter",
                    "msg": where,
                })
    return errors


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": "cargo-check", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return

    file = sys.argv[1]
    start = time.time()

    if not spawnable("cargo"):
        emit(absent(TOOL, file, INSTALL_HINT,
                    int((time.time() - start) * 1000)))
        return

    crate_root = _find_crate_root(file)
    if crate_root is None:



        emit(skipped(TOOL, file,
                     "no Cargo.toml above this file — it belongs to no crate, "
                     "so it was NOT compiled",
                     int((time.time() - start) * 1000)))
        return

    try:
        r = subprocess.run(
            [argv0("cargo"), "check", "--message-format=short", "--quiet"],
            capture_output=True, text=True, timeout=120,
            cwd=str(crate_root), encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:



        emit(absent(TOOL, file, "cargo on PATH but could not be executed — "
                                "this file was NOT compiled",
                    int((time.time() - start) * 1000)))
        return
    except subprocess.TimeoutExpired:
        emit({"tool": "cargo-check", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "timeout (cargo check exceeded 120s)"}],
              "duration_ms": int((time.time() - start) * 1000)})
        return

    dur = int((time.time() - start) * 1000)

    if r.returncode == 0:
        emit({"tool": "cargo-check", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": dur, **COUNT_CONTRACT})
        return

    output = r.stderr or r.stdout or ""



    ws_root, ws_reason = _workspace_root(crate_root)
    errors = _parse_errors(output, file, ws_root=ws_root, ws_reason=ws_reason)
    if not errors:












        errors = [{"line": None, "col": None, "severity": "error",
                   "code": "adapter",
                   "msg": tool_fault("cargo check", r.returncode, output)}]

    emit({"tool": "cargo-check", "file": file, "ok": False, "count": len(errors),
          "errors": errors, "duration_ms": dur, **COUNT_CONTRACT})


if __name__ == "__main__":
    guard_main(TOOL, main)
