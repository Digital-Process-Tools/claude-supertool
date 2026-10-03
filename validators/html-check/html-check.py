#!/usr/bin/env python3






























from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "common"))
from source_context import context_fields
from refusal import absent, guard_main, tool_fault, skipped
from spawnable import argv0, spawnable
from linebreaks import lf_line_of_v8_line

TIMEOUT_S = 30

































































































SCRIPT_OPEN = re.compile(r"<script(?=[\s/>])", re.IGNORECASE)
SCRIPT_CLOSE = re.compile(r"</script(?=[\s/>])", re.IGNORECASE)
QUOTES = ('"', "'")


class UndelimitedTag(Exception):














    def __init__(self, line: int, cause: str, kind: str = "start") -> None:
        super().__init__(f"unterminated <script> {kind} tag at line {line}: {cause}")
        self.line = line
        self.cause = cause
        self.kind = kind


class UnclosedBlock(Exception):
















    def __init__(self, line: int) -> None:
        super().__init__(f"unclosed <script> element opened at line {line}")
        self.line = line


UNCLOSED_QUOTE = "an attribute value opens with a quote that is never closed"
NO_GT = "the file ends before any `>` closes it"


def _parse_tag(html: str, i: int) -> tuple[int, dict[str, str]] | str:




































    n = len(html)
    attrs: dict[str, str] = {}
    j = i
    while j < n:
        c = html[j]
        if c == ">":
            return j + 1, attrs
        if c.isspace() or c == "/":
            j += 1
            continue

        start = j
        while j < n and not (html[j].isspace() or html[j] in "=>/"):
            j += 1
        name = html[start:j].lower()

        k = j
        while k < n and html[k].isspace():
            k += 1
        if k >= n or html[k] != "=":
            attrs.setdefault(name, "")
            continue

        k += 1
        while k < n and html[k].isspace():
            k += 1
        if k < n and html[k] in QUOTES:
            close = html.find(html[k], k + 1)
            if close == -1:
                return UNCLOSED_QUOTE
            attrs.setdefault(name, html[k + 1:close])
            j = close + 1
        else:
            start = k
            while k < n and not (html[k].isspace() or html[k] == ">"):
                k += 1
            attrs.setdefault(name, html[start:k])
            j = k
    return NO_GT


























JS_TYPES = {
    "", "text/javascript", "application/javascript", "module",
    "application/ecmascript", "text/ecmascript",
}





LOCATION = re.compile(r"^(?!\s)(?!node:)(.+?):(\d+)$", re.MULTILINE)
BANNER = re.compile(r"\bSyntaxError\b")


def _script_type(attrs: dict[str, str]) -> str:
    return attrs.get("type", "").strip().lower()


def extract_js_blocks(html: str) -> list[tuple[int, str]]:






    blocks: list[tuple[int, str]] = []
    pos = 0
    while True:
        m = SCRIPT_OPEN.search(html, pos)
        if m is None:
            return blocks
        parsed = _parse_tag(html, m.end())
        if isinstance(parsed, str):
            raise UndelimitedTag(html.count("\n", 0, m.start()) + 1, parsed, "start")
        body_start, attrs = parsed
        close = SCRIPT_CLOSE.search(html, body_start)
        if close is None:












            raise UnclosedBlock(html.count("\n", 0, m.start()) + 1)




        closed = _parse_tag(html, close.end())
        if isinstance(closed, str):
            raise UndelimitedTag(
                html.count("\n", 0, close.start()) + 1, closed, "end")
        close_end, _junk_attrs = closed
        body = html[body_start:close.start()]
        pos = close_end
        if "src" in attrs:
            continue
        if _script_type(attrs) not in JS_TYPES:
            continue
        if not body.strip():
            continue
        blocks.append((html.count("\n", 0, body_start) + 1, body))


def diagnostic_line(out: str, temp_path: str) -> int | None:
    target = os.path.normcase(os.path.realpath(temp_path))
    for m in LOCATION.finditer(out):
        if os.path.normcase(os.path.realpath(m.group(1))) == target:
            return int(m.group(2))
    return None


def spoke_about_file(out: str, line: int | None) -> bool:
    return line is not None or bool(BANNER.search(out))


def check_block(start_line: int, content: str, html_file: str) -> dict | None:

    padded = ("\n" * (start_line - 1)) + content
    fd, temp_path = tempfile.mkstemp(suffix=".js")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(padded)
        try:
            r = subprocess.run([argv0("node"), "--check", temp_path],
                                capture_output=True, text=True, timeout=TIMEOUT_S,
                                encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            return {"line": None, "col": None, "severity": "error", "code": "adapter",
                    "msg": f"timeout — node --check did not return within {TIMEOUT_S}s "
                           f"for the <script> block starting at line {start_line}"}
        if r.returncode == 0:
            return None
        out = (r.stderr or "") + (r.stdout or "")
        line = diagnostic_line(out, temp_path)
        if spoke_about_file(out, line):
            msg_m = re.search(r"((?:Syntax)?Error: .+)", out)
            msg = msg_m.group(1) if msg_m else " ".join(out.split())[:200]







            placed = lf_line_of_v8_line(padded, line) if line is not None else None
            if line is not None and placed is None:

                msg = (msg[:140] + f" [node reported line {line} of the "
                       f"<script> block opened at line {start_line}; no line "
                       "of this page maps to it, so the location is not "
                       "published]")
            err = {"line": placed, "col": None, "severity": "error",
                   "code": "syntax", "msg": msg[:300]}
            if placed is not None:
                err.update(context_fields(html_file, placed))
            return err
        return {"line": None, "col": None, "severity": "error", "code": "adapter",
                "msg": tool_fault("node --check", r.returncode, out)}
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass


def emit(d: dict) -> None:
    print(json.dumps(d))


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": "html-check", "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0})
        return

    file = sys.argv[1]
    start = time.time()

    if not spawnable("node"):
        emit(absent("html-check", file,
                    "node not on PATH — inline <script> blocks were NOT checked",
                    int((time.time() - start) * 1000)))
        return

    try:
        html = pathlib.Path(file).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        emit({"tool": "html-check", "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": f"could not read file: {exc}"}],
              "duration_ms": int((time.time() - start) * 1000)})
        return

    try:
        found = extract_js_blocks(html)
    except UndelimitedTag as exc:











        emit(skipped("html-check", file,
                     f"the <script> {exc.kind} tag at line {exc.line} is never "
                     f"closed ({exc.cause}), so where it ends cannot be told -- "
                     f"NO inline <script> block in this file was checked",
                     int((time.time() - start) * 1000)))
        return
    except UnclosedBlock as exc:






        emit(skipped("html-check", file,
                     f"the <script> element opened at line {exc.line} has no "
                     f"closing tag anywhere after it, so its body runs to the "
                     f"end of the file and whether the file is complete cannot "
                     f"be told -- NO inline <script> block in this file was "
                     f"checked",
                     int((time.time() - start) * 1000)))
        return











    errors = []
    for start_line, content in found:
        err = check_block(start_line, content, file)
        if err is not None:
            errors.append(err)

    dur = int((time.time() - start) * 1000)
    emit({"tool": "html-check", "file": file, "ok": len(errors) == 0,
          "count": len(errors), "errors": errors, "duration_ms": dur})


if __name__ == "__main__":
    guard_main("html-check", main)
