#!/usr/bin/env python3










from __future__ import annotations

import fnmatch
import json
import os
import subprocess
import sys
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "common"))
from source_context import context_fields
from refusal import absent, guard_main, skipped
from spawnable import argv0, spawnable
from linebreaks import split_lines
from path_anchor import (anchor as _anchor, safe_realpath as _safe_realpath,
                          anchor_miss_message as _anchor_miss_message)

TOOL = "markdownlint"
INSTALL_HINT = ("markdownlint not found on PATH — this file was NOT linted "
                "(`npm install -g markdownlint-cli`)")







CHANGELOG_FRAGMENT_GLOB_ENV = "SUPERTOOL_MARKDOWNLINT_CHANGELOG_GLOB"
CHANGELOG_FRAGMENT_GLOB_DEFAULT = "*changelog.d/*.md"






CHANGELOG_ASSEMBLER_ENV = "SUPERTOOL_CHANGELOG_ASSEMBLER"
CHANGELOG_ASSEMBLER_LOCATIONS = (
    os.path.join(".github", "scripts", "assemble_changelog.py"),
    os.path.join(".oss", "assemble_changelog.py"),
    os.path.join("scripts", "assemble_changelog.py"),
)

CHANGELOG_FRAGMENT_SKIP_REASON = (
    "this path matches the project's changelog-fragment convention and its "
    "own assembler script was found above it -- the `changelog-fragment` "
    "validator already owns this file's shape (no heading, no wrap), so "
    "markdownlint's generic ruleset does not apply here (#2338)")


def _repo_root(start: str) -> str | None:





    start_dir = os.path.dirname(os.path.abspath(start)) or "."
    try:
        r = subprocess.run(
            ["git", "-C", start_dir, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    top = r.stdout.strip()
    return top or None


def _changelog_fragment_glob() -> str:
    override = os.environ.get(CHANGELOG_FRAGMENT_GLOB_ENV, "").strip()
    return override if override else CHANGELOG_FRAGMENT_GLOB_DEFAULT


def _assembler_locations() -> tuple:
    override = os.environ.get(CHANGELOG_ASSEMBLER_ENV, "").strip()
    return (override,) if override else CHANGELOG_ASSEMBLER_LOCATIONS


def _owned_by_changelog_fragment(file: str) -> bool:























    norm = os.path.abspath(file).replace(os.sep, "/")
    if not fnmatch.fnmatch(norm, _changelog_fragment_glob()):
        return False
    root = _repo_root(file)
    if root is None:
        return False
    root = os.path.realpath(root)
    current = os.path.dirname(os.path.realpath(file))
    locations = [loc for loc in _assembler_locations() if loc]
    while True:
        for loc in locations:
            if os.path.isfile(os.path.join(current, loc)):
                return True
        if os.path.normcase(current) == os.path.normcase(root):
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return False






TIMEOUT_S = 30




























NOTHING_LINTED_REASON = (
    "markdownlint exited 0 and printed to stdout, which is what it does when "
    "the path resolved to no files to lint — an ignore-file match "
    "(`.markdownlintignore`, `--ignore-path`) or a path that is not there — "
    "so this run is not a verdict about the file: ")


def emit(d: dict) -> None:
    print(json.dumps(d))


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": "markdownlint", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return

    file = sys.argv[1]
    start = time.time()

    if _owned_by_changelog_fragment(file):
        emit(skipped(TOOL, file, CHANGELOG_FRAGMENT_SKIP_REASON,
                     int((time.time() - start) * 1000)))
        return

    if not spawnable("markdownlint"):
        emit(absent(TOOL, file, INSTALL_HINT,
                    int((time.time() - start) * 1000)))
        return

    try:
        result = subprocess.run(
            [argv0("markdownlint"), "--", file],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:



        emit(absent(TOOL, file, "markdownlint on PATH but could not be "
                                "executed — this file was NOT linted",
                    int((time.time() - start) * 1000)))
        return
    except subprocess.TimeoutExpired:




        emit({"tool": "markdownlint", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter",
                          "msg": f"timeout — markdownlint did not return within {TIMEOUT_S}s; "
                                 "the file was NOT checked"}],
              "duration_ms": int((time.time() - start) * 1000)})
        return

    duration = int((time.time() - start) * 1000)

    if result.returncode == 0:
        chatter = (result.stdout or "").strip()
        if chatter:
            lines = split_lines(chatter)
            emit(skipped(TOOL, file,
                         NOTHING_LINTED_REASON + lines[0].strip()[:200],
                         duration))
            return
        emit({"tool": "markdownlint", "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": duration})
        return
























    errors = []
    real = _safe_realpath(file)
    extra = [real] if real and real != file else []
    pattern = _anchor(
        file,
        r":(\d+)(?::(\d+))?\s+(?:(?:error|warning)\s+)?(MD\d+[^\s]*)\s+(.+)$",
        extra_paths=extra)
    output = (result.stdout + result.stderr).strip()
    for line in split_lines(output):
        m = pattern.search(line)
        if m:
            lineno, col, code, msg = m.groups()
            ln = int(lineno)
            err = {
                "line": ln,
                "col": int(col) if col else None,
                "severity": "error",
                "code": code,
                "msg": msg.strip()[:300],
            }
            err.update(context_fields(file, ln))
            errors.append(err)

    if not errors and output:






        errors = [{"line": None, "col": None, "severity": "error",
                   "code": "lint",
                   "msg": _anchor_miss_message(file, output, output[:300])}]

    emit({"tool": "markdownlint", "file": file, "ok": False, "count": len(errors),
          "errors": errors, "duration_ms": duration})


if __name__ == "__main__":
    guard_main(TOOL, main)
