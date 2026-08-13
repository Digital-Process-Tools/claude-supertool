"""One line of a tool's output is one LF, CR or CRLF apart — nothing else (#1486).

`str.splitlines()` also breaks on U+2028, U+2029, U+0085, VT and FF. No analyser
this repo adapts frames its output that way, and most of them echo the source
text they are complaining about verbatim into the diagnostic. So one of those
five characters inside a string literal in the file under validation ends the
adapter's idea of a line in the middle of a diagnostic, the fragment re-matches
the adapter's own regex, and it is published as a second finding.

That is not cosmetic. `count` is the number `_validator_regressed` subtracts, so
a file that mints itself an extra record partly chooses its own baseline
arithmetic — and on a `rollback_on_fail` validator that arithmetic reverts
edits. `go vet` emitted one diagnostic and go-vet reported `count: 2`.

**Use this for anything being PARSED as a line-oriented protocol.**
`str.splitlines()` stays right where the question is "what would some other tool
call a line" — flattening a message for display splits on every separator on
purpose, because that is what neutralises them.

This is `presets/_untrusted.split_lines` (#1081) and `_supertool`'s
`_LINE_BREAK_PATTERN` restated a third time, by necessity: an adapter runs with
`validators/common` on `sys.path`, not the repo root, so it can import neither.
`test_split_lines_matches_the_core_conservative_definition` pins them equal.
"""

from __future__ import annotations

import re

#: `_supertool._LINE_BREAK_PATTERN`, restated. CRLF first: an alternation is
#: ordered, and a bare CR before it would make every CRLF two lines.
LINE_BREAK_PATTERN = r"\r\n|\r|\n"
_LINE_BREAK_RE = re.compile(LINE_BREAK_PATTERN)


def split_lines(text: str) -> list[str]:
    """`text` into lines on LF / CR / CRLF only, endings stripped.

    Matches `str.splitlines()` on a trailing ending — "a" plus one LF is one
    line, not two — so a call site swapping to this does not gain an empty
    last row.
    """
    if not text:
        return []
    parts = _LINE_BREAK_RE.split(text)
    if parts and parts[-1] == "":
        parts.pop()
    return parts


#: ECMAScript's LineTerminator set: `LINE_BREAK_PATTERN` plus U+2028 and U+2029.
#: V8 counts those two, so `node --check` numbers its diagnostics by this set
#: while everything above numbers by the other one (#1507). U+0085, VT and FF
#: are *not* LineTerminators and node agrees — measured on v22.22.1 — so they
#: are absent here even though `str.splitlines()` breaks on all five.
#: Spelled with `chr()` rather than as literal characters: a literal U+2028 in
#: this source would be a line this module's own splitter does not break on and
#: a reader's editor does, which is the defect it exists to describe.
V8_LINE_BREAK_PATTERN = LINE_BREAK_PATTERN + "|" + chr(0x2028) + "|" + chr(0x2029)
_V8_LINE_BREAK_RE = re.compile(V8_LINE_BREAK_PATTERN)


def lf_line_of_v8_line(text: str, v8_line: int) -> int | None:
    """`v8_line` of `text`, re-counted in LF / CR / CRLF lines. None if it has none.

    **Two tools counted the same file differently and the receipt published one
    number under the other's meaning.** One U+2028 inside a string literal —
    legal JavaScript since ES2019, and ordinary in a minified bundle — shifts
    every V8 line number after it by one, while `split_lines`, `context_fields`
    and the reader's editor do not move. Measured on node v22.22.1: a two-line
    file whose first line holds one U+2028 has its line-2 syntax error reported
    as line 3, `context_fields` rendered lines 1-2 with the arrow on neither,
    and the receipt sent a reader to a line the file does not have. Two of them
    make it line 4, so the correction is a count and not an off-by-one.

    `None` is the third state and it is deliberately not a clamp: a number that
    lands past the end of the file under either counting is not a location in
    it, and a caller must publish `line: null` rather than the nearest number
    that exists (`validators/SCHEMA.md`, "A finding you cannot place reports
    `line: null`").

    A file holding neither character maps to itself, which is why this is safe
    to apply unconditionally.
    """
    if v8_line < 1:
        return None
    if v8_line == 1:
        lf_line = 1
    else:
        ends = [m.end() for m in _V8_LINE_BREAK_RE.finditer(text)]
        if len(ends) < v8_line - 1:
            return None
        lf_line = len(_LINE_BREAK_RE.findall(text[:ends[v8_line - 2]])) + 1
    return lf_line if lf_line <= len(split_lines(text)) else None
