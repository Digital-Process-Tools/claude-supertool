
























from __future__ import annotations

import re



LINE_BREAK_PATTERN = r"\r\n|\r|\n"
_LINE_BREAK_RE = re.compile(LINE_BREAK_PATTERN)


def split_lines(text: str) -> list[str]:






    if not text:
        return []
    parts = _LINE_BREAK_RE.split(text)
    if parts and parts[-1] == "":
        parts.pop()
    return parts










V8_LINE_BREAK_PATTERN = LINE_BREAK_PATTERN + "|" + chr(0x2028) + "|" + chr(0x2029)
_V8_LINE_BREAK_RE = re.compile(V8_LINE_BREAK_PATTERN)


def lf_line_of_v8_line(text: str, v8_line: int) -> int | None:





















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
