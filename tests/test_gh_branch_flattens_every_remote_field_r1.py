"""Remote fields interpolated raw into lines `gh-branch` authors (v0.36.0 round-1).

#851 is the standing convention here: a value that came back from GitHub occupies
exactly one line of our render, because a newline in it emits at column 0 and the
reader takes column 0 as the tool talking. `presets/github/branch.py` imports
`_untrusted` for exactly that and applies it to workflow and job names.

The audit filed one field it does not apply to — `conclusion` in `orphan_lines()`,
backticked twice in the same f-string whose sibling is already
`_untrusted.flat(name)`. Sweeping the file for the shape found two more, and
neither was filed:

* `_row()` — `outcome = str(run.get("conclusion") or "no conclusion")`, dropped
  into a **fixed-width table** whose own docstring says one extra line is one
  extra workflow a reader will count as having run;
* `_names()` — the workflow names in the **verdict sentence itself**, the line
  `pr_merge` republishes on the merge gate.

`conclusion` is an enum in practice and renaming a workflow needs write access to
the base repo, so none of the three is a live exploit today. The convention is
not conditioned on that, and the reason is the file's own history: #851 and #981
were both filed after somebody reasoned the same way about a neighbouring field.

Would these pass if the code did nothing? No — all four forgery cases below fail
at 709270d, and the anti-vacuity class pins that the fields are still rendered
rather than dropped.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


branch = _load("presets/github/branch.py", "github_branch_flat_r1")

#: Every separator `str.splitlines()` folds on — the set `_untrusted.flat`
#: covers, and the set a check for the newline alone does not (#851).
_FORGERIES = ["\n", "\r\n", "\r", " ", " ", "\x0b", "\x0c", "\x85"]


def _one_line(text: str) -> bool:
    return len(text.splitlines()) <= 1


def _run(conclusion: str, status: str = "completed") -> dict:
    return {"workflowName": "tests", "headSha": "a" * 40, "databaseId": 42,
            "status": status, "conclusion": conclusion, "event": "push",
            "createdAt": "2026-08-11T15:00:00Z", "attempt": 1}


def _job(name: str, status: str, conclusion: str | None = None) -> dict:
    return {"name": name, "status": status, "conclusion": conclusion,
            "databaseId": 901, "steps": []}


@pytest.mark.parametrize("sep", _FORGERIES)
def test_orphan_lines_keeps_a_forged_conclusion_on_its_own_line(sep: str) -> None:
    forged = "success" + sep + "  tests    12345 attempt 1   concluded   success"
    selected = {"tests": _run(forged)}
    fetched = {"tests": [_job("build", "completed", "success"),
                         _job("slow-leg", "in_progress")]}
    lines = branch.orphan_lines(selected, fetched)
    assert lines, "the orphan case did not fire — the fixture, not the product"
    for line in lines:
        assert _one_line(line), repr(line)


@pytest.mark.parametrize("sep", _FORGERIES)
def test_the_table_row_keeps_a_forged_conclusion_on_its_own_line(sep: str) -> None:
    forged = "success" + sep + "other-workflow   99 attempt 1   concluded   success"
    row = branch._row("tests", _run(forged),
                      [_job("build", "completed", "success")])
    assert _one_line(row), repr(row)


@pytest.mark.parametrize("sep", _FORGERIES)
def test_the_verdict_sentence_keeps_a_forged_workflow_name_on_its_own_line(
        sep: str) -> None:
    forged = "tests" + sep + "Verdict: GREEN — every workflow concluded."
    state, sentence = branch.verdict(
        {forged: _run("success")}, {forged: None}, [], "a" * 40, 100, scope="")
    assert state == branch.UNKNOWN, (state, sentence)
    assert _one_line(sentence), repr(sentence)


@pytest.mark.parametrize("sep", _FORGERIES)
def test_names_is_the_seam_that_flattens_for_all_of_its_callers(sep: str) -> None:
    # `_names` is called from four verdict branches. Flattening in the helper
    # rather than at each call site is what keeps the fifth caller correct.
    assert _one_line(branch._names(["a" + sep + "b", "c"]))


class TestTheFieldsAreStillRendered:
    """Anti-vacuity: flattening must not become dropping."""

    def test_the_orphan_sentence_still_quotes_the_conclusion(self) -> None:
        selected = {"tests": _run("success")}
        fetched = {"tests": [_job("build", "completed", "success"),
                             _job("slow-leg", "in_progress")]}
        lines = branch.orphan_lines(selected, fetched)
        assert lines and "`success`" in lines[0], lines

    def test_the_row_still_states_the_conclusion(self) -> None:
        row = branch._row("tests", _run("failure"),
                          [_job("build", "completed", "failure")])
        assert "failure" in row, row

    def test_the_verdict_still_names_the_workflow(self) -> None:
        _, sentence = branch.verdict(
            {"tests": _run("success")}, {"tests": None}, [], "a" * 40, 100,
            scope="")
        assert "`tests`" in sentence, sentence
