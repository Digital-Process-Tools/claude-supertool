"""Shared before/after line-diff helper for the four SCHEMA-adapter
formatters (`ruff-format`, `php-cs-fixer`, `prettier-write`, `phpcbf` --
#2405, #2458).

Extracted from `ruff-format`'s original `_line_diff` (#2405) so the other
three SCHEMA-adapter formatters can report `metrics.first_changed_line` /
`metrics.last_changed_line` the same way, instead of each adapter carrying
its own copy of a `SequenceMatcher` walk that would drift the moment one of
them got a bugfix the others did not (#2458).
"""
from __future__ import annotations

from difflib import SequenceMatcher
from typing import Optional, Tuple


def line_diff(before: str, after: str) -> Tuple[int, int, Tuple[Optional[int], Optional[int]]]:
    """Return (lines_added, lines_removed, (first_changed_line, last_changed_line)).

    The line-number pair is 1-indexed and refers to `before` -- the file a
    same-session `around_line` read would have seen moments earlier -- so a
    caller can tell whether a formatter's own post-write rewrite reached
    into the region an already-captured read is about to reuse as an
    `edit`'s `old` string, rather than learning only *that* something
    changed (#2405). `(None, None)` when nothing changed at all, never a
    fabricated `(1, 1)` -- the same "must still work when nothing changed"
    control every "must not silently X" claim here needs.

    `SequenceMatcher.get_opcodes()` rather than `unified_diff(..., n=0)`:
    the unified-diff hunk header would need re-parsing to recover line
    numbers, where `get_opcodes()` hands them over as `i1`/`i2` directly.

    Multiple disjoint hunks are reported as ONE merged span (min start,
    max end), not as a list of ranges -- two small, far-apart changes read
    as one range covering everything between them, including untouched
    lines. That over-reports rather than under-reports (a caller re-reading
    the whole span will not miss a touched line), and is the documented
    behaviour, not a bug: see `test_disjoint_hunks_report_one_merged_span_documented_over_approximation`.
    """
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    matcher = SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    added = removed = 0
    first = last = None
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        removed += i2 - i1
        added += j2 - j1
        # A pure insertion (i1 == i2) has no before-side span of its own --
        # anchor it to the nearest existing before-line so the range still
        # names a line number a caller's earlier read would recognise. When
        # `before_lines` is empty there IS no existing before-line to anchor
        # to at all (the whole file is new content) -- the loop below still
        # runs, but its result is discarded after the loop (#2405 self-review:
        # `max(i1, 1) == 1` used to fabricate `(1, 1)` for this case, exactly
        # the value the return-type note below disclaims).
        start = i1 + 1 if i2 > i1 else max(i1, 1)
        end = i2 if i2 > i1 else max(i1, 1)
        if first is None or start < first:
            first = start
        if last is None or end > last:
            last = end
    if not before_lines:
        # Nothing existed before this write -- there is no before-line for
        # any earlier same-session read to have gone stale against, so the
        # touched-range fields must say "nothing to report", not "line 1".
        # This is deliberately a single merged span, not a list of hunks:
        # two small, far-apart changes (e.g. before-lines 2 and 10 of a
        # 10-line file) are reported as one span covering everything
        # between them. That over-reports rather than under-reports -- a
        # caller re-reading the whole span will not miss a touched line --
        # and a list-of-ranges return shape is more machinery than #2405's
        # own repro (one contiguous collapsed block) calls for.
        first = last = None
    return added, removed, (first, last)
