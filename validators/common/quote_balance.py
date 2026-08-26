"""Best-effort bash quote-balance tracker (#1810).

`bash -n` reports a syntax finding at the line where its parser gave up, which
for an unterminated `'` or `"` is not the line that opened it -- the reported
incident put five lines and a whole indented `function` block between the two,
in a ~1,900-line file where every neighbouring line is also full of quotes.
This module answers a narrower, checkable question instead of trying to be
`bash -n`: **is a quote still open by the time execution reaches line N, and
if so, where did it start?**

This is NOT a bash parser and does not try to be one. It has no model of
here-docs, command substitution, ANSI-C $'...' quoting, or nested
constructs -- any of those can make its answer wrong. Nor does it require a
`#` to sit at a word boundary the way real bash does, so `foo#bar'baz` reads
as a comment starting at the `#` here and would not in a real shell -- a false
negative (no guess where one exists), never a false positive, so it degrades
to silence rather than to a wrong pointer. That is exactly why its result
must never be folded into `line`, `col` or `msg` (bash's own, and exact):
callers surface it as a separately labelled guess, never as a second
diagnostic.
"""
from __future__ import annotations

from linebreaks import split_lines


def unbalanced_quote_open(text: str, upto_line: int) -> int | None:
    """1-based line where a quote still open at `upto_line` began, or None.

    Walks `text` from its first line through `upto_line` (inclusive) tracking
    quote state character by character: `#` starts a comment when unquoted (to
    end of line), a backslash escapes the next character when unquoted or
    inside a double-quoted string (never inside a single-quoted one, matching
    bash), and a matching quote character closes the string it opened.

    Returns None when nothing is open at `upto_line` -- either no quote was
    ever opened, or every one seen was closed again before that point.
    """
    lines = split_lines(text)
    state: str | None = None
    open_line: int | None = None
    for i, line in enumerate(lines, start=1):
        if i > upto_line:
            break
        j = 0
        n = len(line)
        while j < n:
            ch = line[j]
            if state is None:
                if ch == "#":
                    break
                if ch == "\\":
                    j += 2
                    continue
                if ch == "'":
                    state, open_line = "'", i
                elif ch == '"':
                    state, open_line = '"', i
                j += 1
                continue
            if state == "'":
                if ch == "'":
                    state, open_line = None, None
                j += 1
                continue
            # state == '"'
            if ch == "\\":
                j += 2
                continue
            if ch == '"':
                state, open_line = None, None
            j += 1
    return open_line
