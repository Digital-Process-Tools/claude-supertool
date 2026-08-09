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

# A block is <script ATTRS>BODY</script ANYTHING>, case-insensitive, BODY may
# span lines. The two ends are delimited differently and both comments below
# say why: the end tag by SCRIPT_CLOSE, the start tag by `_parse_start_tag`.
#
# The end tag allows `[^>]*` before its `>` and not `\s*`: an HTML end tag may carry
# whitespace and junk attributes before its `>` (`</script bar>`), and every
# parser ignores the junk and closes the element. Matching only whitespace
# meant the closing tag was not found at all -- so the non-greedy body either
# paired with the *next* block's close and handed node a slab of markup, or
# found no close and dropped the block entirely, leaving the file `ok` with
# broken JS still in it. That is the silent gap #833 exists to close, arriving
# through the one pattern that decides what gets looked at. The lookahead keeps
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
# The *start* tag is not a regex, and that is #1153. `<script\b([^>]*)>` stops
# at the first `>` in the source, but a `>` inside a quoted attribute value is
# not the end of a tag -- `<script data-tpl="a > b">` ends at the second one.
# Truncating there is not merely short: it stops *inside* a quoted value, so
# the value's own text is then read as attribute syntax, and it fails in both
# directions at once.
#
#   <script data-tpl="a > b" type="application/json">   -> read as JavaScript,
#       because the `type` naming it as data sits past the cut. node is handed
#       a JSON payload and reports a syntax error about it. False positive.
#   <script data-tpl="foo src=1 > b">BROKEN JS</script> -> read as external,
#       because ` src=` *inside the value* is all the raw-text `src=` pattern
#       can see. The block is dropped and the file reports `ok` with the
#       broken JS still in it --
#       the same absence-read-as-presence the end-tag fix above exists to
#       close, arriving through the other half of the same pattern.
#
# `_parse_start_tag` walks the tag instead. This is not "a better regex until
# the reported case passes": the start-tag grammar really is a small state
# machine, `>` can only hide inside a quoted value, and a quote only opens
# such a value directly after `=`. Anywhere else a quote is an ordinary
# character (`<script data-x=a"b>` is a parse error whose value is `a"b` and
# whose tag ends at the `>`), and treating it as an opener would run to the
# next quote in the file and lose the block -- the loud bug traded for a
# quiet one. Verified against html.parser, which is spec-conformant here.
#
# Where the tag genuinely cannot be delimited -- a quoted value with no
# closing quote, or a file that stops mid-tag, both EOF-in-tag, where the
# tokenizer drops the tag outright -- there is nothing to be smarter about,
# and the answer is the third state, naming which of the two it met.
# See docs/validators.md, "Declining instead of guessing".
#
# Tag names end at whitespace, `/` or `>`. `\b` also ended one at a hyphen, so
# `<script-foo>` was extracted as a script block and its children handed to
# node: a syntax error reported against markup on a page whose JS is fine.
SCRIPT_OPEN = re.compile(r"<script(?=[\s/>])", re.IGNORECASE)
SCRIPT_CLOSE = re.compile(r"</script(?=[\s/>])[^>]*>", re.IGNORECASE)
QUOTES = ('"', "'")


class UndelimitedTag(Exception):
    """A `<script` start tag with no end.

    Carries the 1-indexed line it opens on and *which* of the two ways it
    failed to close, because they send the reader to different places: a
    value with no closing quote is a typo on that line, a file that stops
    mid-tag is a truncated file. A refusal naming the wrong one is the
    defect this adapter's third state exists to remove, one layer out.
    """

    def __init__(self, line: int, cause: str) -> None:
        super().__init__(f"unterminated <script> start tag at line {line}: {cause}")
        self.line = line
        self.cause = cause


UNCLOSED_QUOTE = "an attribute value opens with a quote that is never closed"
NO_GT = "the file ends before any `>` closes it"


def _parse_start_tag(html: str, i: int) -> tuple[int, dict[str, str]] | str:
    """Walk the start tag whose name ends at `html[i]`. `(end, attributes)`.

    `end` is the index just past the tag's `>`; `attributes` maps lowercased
    name to value, first occurrence winning, as the spec has it.

    A `str` return is a refusal carrying its own cause: the tag has no end,
    so where it stops -- and therefore where its body starts, or whether it
    has one -- is not knowable from this input.

    Agreement with `html.parser` is measured, not assumed, and the bound is
    worth stating exactly. On well-formed start tags the parsed attributes
    match it outright. On tags malformed in before-attribute-name position
    (`<script ="v">`, a bare quote where a name belongs) the two disagree
    about what the *name* is -- and so, incidentally, do html.parser and the
    WHATWG spec. What is measured over ~60k such tags is the property this
    adapter actually rests on: where html.parser parses the tag at all, we
    never answer differently about whether `src` is present or what `type`
    holds. We only decline. That is the one direction a validator may be
    wrong in.
    """
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


# `src` and `type` are read off the parsed attributes above rather than
# matched against the tag's raw text, and that is not a tidy-up.
#
# The raw-text patterns were `(?:^|\s)src\s*=` and its `type` twin, anchored
# on whitespace because a bare `\b` also fires on the hyphen in `data-src=` /
# `data-type=` -- the exact names consent-management scripts (OneTrust,
# Cookiebot) use to stash the real src/type while `src`/`type` point
# elsewhere. But an anchor only tells `data-src=` from `src=`. It cannot tell
# an attribute from a *string*, and a quoted value is ordinary text:
#
#   <script data-tpl="foo src=1 bar">BROKEN JS</script>
#
# has no `src` attribute, no `>` anywhere it should not be, and a tag that
# delimits perfectly -- and was still dropped as external, reporting `ok`
# with the broken JS in it. Nothing about #1153's `>` is needed to reach it;
# `>` only made it easier to hit, by cutting the tag mid-value so the value's
# text became the whole attribute string. Parsing removes the class rather
# than the instance: `data-src` is a different name, and the contents of a
# value are never names at all.

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


def _script_type(attrs: dict[str, str]) -> str:
    return attrs.get("type", "").strip().lower()


def extract_js_blocks(html: str) -> list[tuple[int, str]]:
    """(start_line, content) for every inline, JS-typed <script> block.

    `start_line` is the 1-indexed line of the block's own first character,
    so padding the content with `start_line - 1` leading newlines makes
    node's line numbers land on the original file's line numbers directly.
    """
    blocks: list[tuple[int, str]] = []
    pos = 0
    while True:
        m = SCRIPT_OPEN.search(html, pos)
        if m is None:
            return blocks
        parsed = _parse_start_tag(html, m.end())
        if isinstance(parsed, str):
            raise UndelimitedTag(html.count("\n", 0, m.start()) + 1, parsed)
        body_start, attrs = parsed
        close = SCRIPT_CLOSE.search(html, body_start)
        if close is None:
            # No close anywhere after this open: not a block. Resume past the
            # start tag rather than past the file, so a later, well-formed
            # block is still found -- what the non-greedy pattern did too.
            pos = body_start
            continue
        body = html[body_start:close.start()]
        pos = close.end()
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

    try:
        found = extract_js_blocks(html)
    except UndelimitedTag as exc:
        # The whole file, not just this tag. A verdict naming one finding
        # would be a claim about a file that was not read past line
        # `exc.line`, and `ok`/`count` are exactly that claim. Nothing is
        # lost by declining: the reason names the line to fix first, and
        # anything else wrong with the file is still there on the next run.
        emit(skipped("html-check", file,
                     f"the <script> start tag at line {exc.line} is never closed "
                     f"({exc.cause}), so where it ends cannot be told -- "
                     f"NO inline <script> block in this file was checked",
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
    main()
