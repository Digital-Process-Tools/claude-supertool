"""#1073 - `grep` said TRUNCATED without saying how truncated.

`(20 results ... limit 20 - TRUNCATED, more matches exist)` is honest about the
answer being partial and silent about its scope. 21 matches and 500 matches
produce identical bytes and warrant opposite next actions: at 21 you raise the
limit and read them, at 500 the pattern is wrong. The sibling renders already
carry a denominator (`read` prints `lines 1-92 of 399`, `gh-issues` prints
`N of M fetched`); this one carried a word.

Three states, and the tests keep them apart:

* counted, exact          -> `TRUNCATED, N matches total`
* counted, hit the ceiling -> `TRUNCATED, N+ matches total (count capped at N)`
* not counted at all       -> `TRUNCATED, more matches exist (total not counted)`

The last one is the rtk-delegated report, which has no candidate list to count
over. "We did not count" must not render as "we counted and there are some".

The happy path is pinned too: a complete answer prints no total and no marker,
so the marker's absence stays a positive statement that the count is exact.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import _supertool


def _tree(tmp_path: Path, matches: int, files: int = 1) -> str:
    """A directory of .py files holding exactly `matches` matching lines."""
    d = tmp_path / "tree"
    d.mkdir()
    per, extra = divmod(matches, files)
    for i in range(files):
        n = per + (1 if i < extra else 0)
        body = "".join(f"needle {i} {j}\n" for j in range(n))
        (d / f"f{i}.py").write_text(body, encoding="utf-8")
    return str(d)


def _header(out: str) -> str:
    return out.splitlines()[0]


def test_truncated_grep_states_the_total(tmp_path: Path) -> None:
    out = _supertool.op_grep("needle", _tree(tmp_path, 30), limit=5,
                             no_auto_read=True)
    head = _header(out)
    assert "TRUNCATED" in head, head
    assert "30 matches total" in head, head


def test_total_is_exact_one_past_the_limit(tmp_path: Path) -> None:
    """The boundary the old `limit + 1` probe could see but not report."""
    head = _header(_supertool.op_grep("needle", _tree(tmp_path, 6), limit=5,
                                      no_auto_read=True))
    assert "6 matches total" in head, head


def test_total_counts_across_files(tmp_path: Path) -> None:
    head = _header(_supertool.op_grep("needle", _tree(tmp_path, 40, files=8),
                                      limit=5, no_auto_read=True))
    assert "40 matches total" in head, head


def test_complete_answer_prints_no_total_and_no_marker(tmp_path: Path) -> None:
    """A count that appears only on truncation is fine; a wrong one on the
    happy path would be a new defect."""
    head = _header(_supertool.op_grep("needle", _tree(tmp_path, 4), limit=10,
                                      no_auto_read=True))
    assert "TRUNCATED" not in head, head
    assert "matches total" not in head, head
    assert "4 results" in head, head


def test_count_stops_at_the_ceiling_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counting every match forfeits the early exit, so the count is bounded -
    and a bounded count must not be printed as an exact one."""
    monkeypatch.setenv("SUPERTOOL_GREP_COUNT_CEILING", "10")
    head = _header(_supertool.op_grep("needle", _tree(tmp_path, 30), limit=5,
                                      no_auto_read=True))
    assert "10+ matches total" in head, head
    assert "count capped at 10" in head, head
    assert "30" not in head.split("limit")[-1], head


def test_ceiling_below_the_limit_never_shrinks_the_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ceiling under the caller's own limit would cap the count below the
    number of rows printed under it."""
    monkeypatch.setenv("SUPERTOOL_GREP_COUNT_CEILING", "2")
    out = _supertool.op_grep("needle", _tree(tmp_path, 30), limit=5,
                             no_auto_read=True)
    head = _header(out)
    assert head.count("needle") == 0
    assert "5 results" in head, head
    rows = [ln for ln in out.splitlines() if "needle" in ln]
    assert len(rows) == 5, out


def test_context_mode_states_the_total_too(tmp_path: Path) -> None:
    head = _header(_supertool.op_grep("needle", _tree(tmp_path, 30), limit=5,
                                      context=1, no_auto_read=True))
    assert "TRUNCATED" in head, head
    assert "30 matches total" in head, head


def test_context_mode_complete_answer_has_no_total(tmp_path: Path) -> None:
    head = _header(_supertool.op_grep("needle", _tree(tmp_path, 3), limit=10,
                                      context=1, no_auto_read=True))
    assert "TRUNCATED" not in head, head
    assert "matches total" not in head, head


def test_delegated_report_says_the_total_was_not_counted() -> None:
    """rtk answers without a candidate list, so there is nothing to count over.
    Not-counted is a third state, not a quiet version of counted."""
    rtk_out = "\n".join(f"a.py:{i}:needle" for i in range(1, 8))
    head = _header(_supertool._rtk_grep_report(rtk_out, limit=3))
    assert "TRUNCATED" in head, head
    assert "total not counted" in head, head
    assert "matches total" not in head, head


def test_truncation_suffix_keeps_its_three_states() -> None:
    f = _supertool._truncation_suffix
    assert f(False) == ""
    assert f(False, total=99) == ""
    assert "137 matches total" in f(True, total=137)
    assert "500+ matches total" in f(True, total=500, capped=True)
    assert "total not counted" in f(True)
