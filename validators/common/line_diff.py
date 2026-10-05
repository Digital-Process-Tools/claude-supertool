









from __future__ import annotations

from difflib import SequenceMatcher
from typing import Optional, Tuple

from linebreaks import split_lines


def line_diff(before: str, after: str) -> Tuple[int, int, Tuple[Optional[int], Optional[int]]]:































    before_lines = split_lines(before)
    after_lines = split_lines(after)
    matcher = SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    added = removed = 0
    first = last = None
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        removed += i2 - i1
        added += j2 - j1








        start = i1 + 1 if i2 > i1 else max(i1, 1)
        end = i2 if i2 > i1 else max(i1, 1)
        if first is None or start < first:
            first = start
        if last is None or end > last:
            last = end
    if not before_lines:










        first = last = None
    return added, removed, (first, last)
