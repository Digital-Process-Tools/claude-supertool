




















from __future__ import annotations

from linebreaks import split_lines


def unbalanced_quote_open(text: str, upto_line: int) -> int | None:











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

            if ch == "\\":
                j += 2
                continue
            if ch == '"':
                state, open_line = None, None
            j += 1
    return open_line
