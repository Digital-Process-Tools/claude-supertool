"""#1409 — `gh-branch` named workflows and never named runs.

The table's `Run` column held a lifecycle word (`concluded` / `running` /
`unestablished`) and nothing else, so the render carried no run id at all. Two
consequences, both measured while diagnosing an orphaned check-run on #1406:

* **No route onward.** `gh-run:<id>` and `gh run rerun <id>` both need the id,
  and getting one meant a raw API call — which is the standing tell that an op
  is missing a field, not that the caller should reach past it.
* **A re-run was invisible.** GitHub's `run_attempt` is already fetched by
  `_run_list` and was already read by `_declared_legs.reconcilable`, which
  exists precisely because attempt 2 puts legs in `filter=all` that
  `filter=latest` has dropped. The reader of the table had no way to know the
  tally beside a row was one attempt of several.

The issue body asked for a new op and its own author retracted that: the
resolution, the `--commit` exact-match trap and the declared-set comparison all
already live here (#1083, #846). This is two fields on the render that exists.

`attempt` is rendered **always**, never only when it is greater than one: a
number that appears only in the interesting case cannot be told from a number
the tool failed to read, which is this repository's house defect in miniature.
An unreadable id or attempt renders `?`, never a default.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


branch = _load("presets/github/branch.py", "github_branch_1409")


def _run(**over: Any) -> dict:
    base = {"status": "completed", "conclusion": "success",
            "databaseId": 31501780284, "attempt": 1,
            "createdAt": "2026-08-11T00:00:00Z"}
    base.update(over)
    return base


_PASS = [{"name": "leg", "status": "completed", "conclusion": "SUCCESS"}]


# ---------------------------------------------------------------------------
# the cell
# ---------------------------------------------------------------------------

class TestRunCell:

    def test_the_run_id_is_rendered(self) -> None:
        assert "31501780284" in branch.run_cell(_run())

    def test_attempt_one_is_stated_rather_than_implied(self) -> None:
        """An absent number and an unread number must not look alike."""
        assert "attempt 1" in branch.run_cell(_run(attempt=1))

    def test_a_rerun_is_visible_and_differs_from_the_first_attempt(self) -> None:
        first = branch.run_cell(_run(attempt=1))
        again = branch.run_cell(_run(attempt=2))
        assert first != again
        assert "attempt 2" in again

    def test_an_absent_attempt_is_a_question_mark_not_a_one(self) -> None:
        cell = branch.run_cell(_run(attempt=None))
        assert "attempt ?" in cell, cell
        assert "attempt 1" not in cell, cell

    def test_an_unreadable_attempt_is_a_question_mark(self) -> None:
        assert "attempt ?" in branch.run_cell(_run(attempt="two"))

    def test_an_absent_run_id_never_renders_as_minus_one(self) -> None:
        """`_run_id` answers -1 for an unreadable id. -1 is not a run."""
        cell = branch.run_cell(_run(databaseId=None))
        assert "-1" not in cell, cell
        assert "?" in cell, cell

    def test_a_non_dict_run_is_declined_not_crashed(self) -> None:
        assert "?" in branch.run_cell(None)


# ---------------------------------------------------------------------------
# the row, which is what a reader sees
# ---------------------------------------------------------------------------

class TestRow:

    def test_the_row_carries_the_id_and_the_attempt(self) -> None:
        row = branch._row("tests", _run(attempt=3), _PASS)
        assert "31501780284" in row, row
        assert "attempt 3" in row, row

    def test_the_row_still_carries_the_lifecycle_phase(self) -> None:
        """The column split must not cost the word #615 comment 1 asked for."""
        assert branch.PHASE_CONCLUDED in branch._row("tests", _run(), _PASS)
        assert branch.PHASE_RUNNING in branch._row(
            "tests", _run(status="in_progress", conclusion=""), _PASS)

    def test_the_row_still_carries_the_outcome_and_the_tally(self) -> None:
        row = branch._row("tests", _run(), _PASS)
        assert "success" in row, row
        assert "1 total" in row, row


# ---------------------------------------------------------------------------
# the header and the note, so the numbers are readable as what they are
# ---------------------------------------------------------------------------

class TestDisclosure:

    def test_the_header_names_both_new_columns(self) -> None:
        head = branch.table_header()
        assert "Run" in head and "Phase" in head, head

    def test_the_header_is_wide_enough_for_the_widest_cell(self) -> None:
        """A column narrower than its content silently runs into its neighbour."""
        cell = branch.run_cell(_run(databaseId=31501780284, attempt=12))
        assert len(cell) <= branch.RUN_COL, (cell, branch.RUN_COL)

    def test_the_note_says_what_an_id_is_for_and_what_an_attempt_means(
            self) -> None:
        note = branch.run_id_note()
        assert "gh-run:" in note, note
        assert "attempt" in note, note
        # The correction the issue body got wrong: a re-run is a further
        # attempt on the same run object, not a second run object, so the row
        # count is workflows and the tally is the latest attempt only.
        assert "latest attempt" in note, note
