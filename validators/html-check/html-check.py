#!/usr/bin/env python3
"""html-check validator adapter — syntax-checks inline JS in <script> blocks.

Stdlib only. Reference implementation per validators/SCHEMA.md.
Usage:  html-check.py <file>

#833: nothing in validators/ matched `*.html`, so a stray brace in an inline
`<script>` shipped to a published dashboard silently. The fix intentionally
is NOT well-formedness checking of the HTML itself — a strict parser rejects
plenty of pages a browser renders fine (unclosed <br>/<img>, optional
closing tags), and trading a quiet gap for a loud false-positive on valid
HTML is worse, not better. What every other validator here promises is a
narrow, tool-backed verdict about one thing; the one thing HTML has that
nothing else sees is inline JS.

So: extract each <script> block that is not external (`src=`) and not a
non-JS payload (`type="application/json"`, `"application/ld+json"`, an
inline template mimetype, ...), and hand its content to `node --check` —
the same mechanism node-check.py already uses for *.js. Each block's
content is padded with as many leading blank lines as its own start line
in the HTML source, so node's own line numbers already ARE the file's line
numbers; no separate offset arithmetic to get wrong.

Tag-balance checking was in the issue's proposal as optional and is left
out here on purpose: it is exactly the well-formedness trap above wearing
a smaller costume, and script-extraction alone closes the gap the issue
reports (never-checked inline JS) without inventing a new false-positive
source. If it turns out to be wanted, it is a second, separately-decided
validator, not a silent addition to this one's contract.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "common"))
from source_context import source_context
from refusal import tool_fault, skipped

TIMEOUT_S = 30

# <script ATTRS>BODY</script ANYTHING>, case-insensitive, BODY may span lines.
#
# The end tag is `\b[^>]*>` and not `\s*>`: an HTML end tag may carry
# whitespace and junk attributes before its `>` (`</script bar>`), and every
# parser ignores the junk and closes the element. Matching only whitespace
# meant the closing tag was not found at all -- so the non-greedy body either
# paired with the *next* block's close and handed node a slab of markup, or
# found no close and dropped the block entirely, leaving the file `ok` with
# broken JS still in it. That is the silent gap #833 exists to close, arriving
# through the one pattern that decides what gets looked at. `\b` keeps
# `</scriptfoo>` out: it is a different tag, not this one with junk on it.
#
# This deliberately closes on a `</script ...>` that sits inside a JS string
# or template literal, and that is not a false positive -- it is the HTML
# tokenizer's own rule. Script data ends at `</script` followed by whitespace,
# `/` or `>`, with no idea that JS strings exist, which is why the way to put
# that text in a script is `<\/script>`. Measured against the stdlib
# tokenizer, which is spec-conformant here:
#
#   <script>const s = "</script data-x>"; const z = 1;</script>
#   html.parser -> data 'const s = "' then endtag script
#
# So a page written that way really does have its script cut off at that
# point in a browser, really is broken, and saying so is the job. The old
# `</script\s*>` agreed with the tokenizer on the bare form and disagreed on
# the form carrying attributes -- one rule, applied to half the cases.
SCRIPT_TAG = re.compile(r"<script\b([^>]*)>(.*?)</script\b[^>]*>", re.IGNORECASE | re.DOTALL)
# Anchored on whitespace/start-of-attrs, not `\b`: a bare word boundary also
# fires on the hyphen in `data-src=` / `data-type=` (the exact attribute
# names consent-management scripts like OneTrust/Cookiebot use to stash the
# real src/type while `src`/`type` point elsewhere), which would misread a
# still-inline, still-real script as external or non-JS and silently skip
# the one gap #833 exists to close.
SRC_ATTR = re.compile(r"(?:^|\s)src\s*=", re.IGNORECASE)
TYPE_ATTR = re.compile(r"""(?:^|\s)type\s*=\s*(?:"([^"]*)"|'([^']*)'|(\S+))""", re.IGNORECASE)

# Absence of a type attribute means JS. An explicit type must name JS (or a
# module) to be checked — anything else (json, ld+json, a template dialect
# like text/x-handlebars-template) is data this validator has no business
# running through a JS parser.
JS_TYPES = {
    "", "text/javascript", "application/javascript", "module",
    "application/ecmascript", "text/ecmascript",
}

# node --check opens a syntax report with the resolved path of the file it
# read, alone on a line — mirrors node-check.py's own LOCATION/BANNER split
# (see that file for the #753 rationale); duplicated rather than imported
# because each per-block temp path is compared, not the html file's own.
LOCATION = re.compile(r"^(?!\s)(?!node:)(.+?):(\d+)$", re.MULTILINE)
BANNER = re.compile(r"\bSyntaxError\b")


def _script_type(attrs: str) -> str:
    m = TYPE_ATTR.search(attrs)
    if not m:
        return ""
    return (m.group(1) or m.group(2) or m.group(3) or "").strip().lower()


def extract_js_blocks(html: str) -> list[tuple[int, str]]:
    """(start_line, content) for every inline, JS-typed <script> block.

    `start_line` is the 1-indexed line of the block's own first character,
    so padding the content with `start_line - 1` leading newlines makes
    node's line numbers land on the original file's line numbers directly.
    """
    blocks: list[tuple[int, str]] = []
    for m in SCRIPT_TAG.finditer(html):
        attrs, body = m.group(1), m.group(2)
        if SRC_ATTR.search(attrs):
            continue
        if _script_type(attrs) not in JS_TYPES:
            continue
        if not body.strip():
            continue
        start_line = html.count("\n", 0, m.start(2)) + 1
        blocks.append((start_line, body))
    return blocks


def diagnostic_line(out: str, temp_path: str) -> int | None:
    target = os.path.normcase(os.path.realpath(temp_path))
    for m in LOCATION.finditer(out):
        if os.path.normcase(os.path.realpath(m.group(1))) == target:
            return int(m.group(2))
    return None


def spoke_about_file(out: str, line: int | None) -> bool:
    return line is not None or bool(BANNER.search(out))


def check_block(start_line: int, content: str, html_file: str) -> dict | None:
    """Run one <script> block through `node --check`. None means clean."""
    padded = ("\n" * (start_line - 1)) + content
    fd, temp_path = tempfile.mkstemp(suffix=".js")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(padded)
        try:
            r = subprocess.run(["node", "--check", temp_path],
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
            err = {"line": line, "col": None, "severity": "error",
                   "code": "syntax", "msg": msg[:300]}
            if line is not None:
                err["source_context"] = source_context(html_file, line)
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

    if not shutil.which("node"):
        emit(skipped("html-check", file,
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

    errors = []
    for start_line, content in extract_js_blocks(html):
        err = check_block(start_line, content, file)
        if err is not None:
            errors.append(err)

    dur = int((time.time() - start) * 1000)
    emit({"tool": "html-check", "file": file, "ok": len(errors) == 0,
          "count": len(errors), "errors": errors, "duration_ms": dur})


if __name__ == "__main__":
    main()
