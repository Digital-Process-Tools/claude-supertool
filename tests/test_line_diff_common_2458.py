"""Unit tests for the shared `validators/common/line_diff.py` helper (#2458).

Extracted from `ruff-format`'s original `_line_diff` (#2405) so the three
other SCHEMA-adapter formatters can share it rather than each carrying its
own copy. These tests exercise the helper directly, independent of any
adapter subprocess -- the per-formatter test files still prove the wiring
(that each adapter actually calls this and plumbs the result into
`metrics.first_changed_line` / `last_changed_line`).
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "validators" / "common"))
from line_diff import line_diff  # noqa: E402


def test_noop_reports_zero_counts_and_no_range() -> None:
    text = "a = 1\nb = 2\n"
    added, removed, (first, last) = line_diff(text, text)
    assert added == 0 and removed == 0
    assert first is None and last is None


def test_contiguous_change_reports_touched_span() -> None:
    before = "def f(x):\n    if (\n        isinstance(x, int)\n        and x > 0\n    ):\n        return x\n    return None\n"
    after = "def f(x):\n    if isinstance(x, int) and x > 0:\n        return x\n    return None\n"
    added, removed, (first, last) = line_diff(before, after)
    assert added > 0 and removed > 0
    assert (first, last) == (2, 5)


def test_insertion_into_empty_file_reports_no_range() -> None:
    """MUST NOT FIRE: a pure insertion into a genuinely empty before-file has
    no existing before-line for a stale caller read to have gone stale
    against -- reporting (1, 1) here would be fabricated (#2405 self-review).
    """
    added, removed, (first, last) = line_diff("", "x = 1\n")
    assert added > 0
    assert first is None and last is None


def test_insertion_before_existing_first_line_reports_it() -> None:
    """MUST FIRE control for the test above: a file that already had content
    really does have a before-line 1 to report as touched.
    """
    added, removed, (first, last) = line_diff("a = 1\nb = 2\n", "x = 0\na = 1\nb = 2\n")
    assert (first, last) == (1, 1)


def test_disjoint_hunks_report_one_merged_span() -> None:
    before = "".join(f"{n}\n" for n in range(1, 11))
    after_lines = list(range(1, 11))
    after_lines[1] = "X"
    after_lines[9] = "Y"
    after = "".join(f"{v}\n" for v in after_lines)
    added, removed, (first, last) = line_diff(before, after)
    assert (first, last) == (2, 10)
