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
