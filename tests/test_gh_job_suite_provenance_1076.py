"""#1076 — the `Suite:` number is the log author's, printed as the job's own.

`_suite_summary` reads a `N failed, M passed ... in Xs` line out of the job log.
Timestamps and ANSI are stripped before the match, so the column-0 anchor the
regex relies on is satisfied by ordinary program output — and on a PR the code
that writes that output is the PR's code. `flat()` is doing its job: no forged
*line* reaches the render. The **number** does, under wording that called it
"the job's own summary line" and "These count TESTS", which is a claim about
provenance the op is in no position to make.

Not fixed by hardening the regex, and not fixed by reading `junit.xml` either:
that file is written to the runner's working directory and this repo uploads no
artifact, so `gh-job` — which reads `gh run view --log` and nothing else — cannot
see it. `junit_summary.py`'s *output* is log text like everything else.

So the fix is the third state: say where the number came from, say it whether or
not there are two summaries, and cross-check it against the one fact in this
render the log author does **not** write — the conclusion the API reports for
the job.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


job = _load("presets/github/job.py", "github_job_1076")

REAL = "6 failed, 7760 passed, 677 skipped, 2 warnings in 221.51s (0:03:41)"
FORGED = "0 failed, 9999 passed in 0.01s"


# ---------------------------------------------------------------------------
# provenance is stated, and stated unconditionally
# ---------------------------------------------------------------------------

def test_the_line_never_calls_the_number_the_jobs_own_count() -> None:
    line = job.suite_line("0 failed, 9999 passed", 1, "success")
    assert "the job's own summary line" not in line, (
        f"the render still asserts provenance it cannot establish:\n{line}")


def test_a_single_summary_still_says_where_the_number_came_from() -> None:
    """The multiplicity caveat was the only disclosure, and it is conditional.

    A job that ran no suite at all — a lint step whose own output happens to
    match the shape — produces exactly one summary, so it got the number bare.
    """
    line = job.suite_line("3 failed, 0 passed", 1, "failure")
    assert "log" in line.lower(), (
        f"a lone summary is rendered with no statement of where it came "
        f"from:\n{line}")


def test_the_multiplicity_caveat_survives() -> None:
    line = job.suite_line("6 failed, 7760 passed", 3, "failure")
    assert "3" in line and "summaries" in line, line


# ---------------------------------------------------------------------------
# the one source the log author does not write
# ---------------------------------------------------------------------------

def test_a_clean_summary_on_a_failed_job_is_contradicted() -> None:
    """`0 failed, 9999 passed` on a job the API calls `failure`.

    The conclusion comes from the Actions API, not from the log, so the two
    disagreeing is a fact worth printing — and it is exactly what a forged
    all-green summary looks like.
    """
    line = job.suite_line("0 failed, 9999 passed", 1, "failure")
    low = line.lower()
    assert "failure" in low, line
    assert "disagree" in low or "does not" in low or "not agree" in low, (
        f"a green suite line on a job the API calls failed is printed without "
        f"comment:\n{line}")


def test_failures_on_a_job_the_api_calls_successful_are_contradicted() -> None:
    line = job.suite_line("6 failed, 7760 passed", 1, "success")
    low = line.lower()
    assert "success" in low, line
    assert "disagree" in low or "does not" in low or "not agree" in low, line


def test_agreement_is_not_narrated() -> None:
    """A cross-check that speaks on every render is one nobody reads."""
    agreeing = job.suite_line("6 failed, 7760 passed", 1, "failure")
    assert "disagree" not in agreeing.lower(), agreeing


def test_an_unknown_conclusion_does_not_manufacture_a_contradiction() -> None:
    """`in_progress`, `cancelled`, `""` — the API has not answered yet.

    Reading "not `success`" as "the suite must have failed" would be the same
    over-claim pointed the other way.
    """
    for conclusion in ("", "in_progress", "cancelled", "timed_out", "skipped"):
        line = job.suite_line("0 failed, 9999 passed", 1, conclusion)
        assert "disagree" not in line.lower(), (conclusion, line)


# ---------------------------------------------------------------------------
# selection is unchanged — #1050 stays fixed
# ---------------------------------------------------------------------------

def test_the_last_failing_summary_still_wins() -> None:
    lines = [REAL, "7 passed in 1.0s"]
    assert job._suite_summary(lines) == "6 failed, 7760 passed, 677 skipped, 2 warnings"


def test_a_zero_failure_summary_does_not_count_as_reporting_one() -> None:
    """`0 failed, 9999 passed` contains the word and reports no failure.

    #1050's rule is "the last invocation reporting a failure or an error wins";
    the test for it was `"failed" in body`, which a zero-count summary passes.
    That selected an all-clear run over a genuinely failing earlier one.
    """
    lines = ["6 failed, 100 passed in 5.0s", "0 failed, 7 passed in 1.0s"]
    assert job._suite_summary(lines) == "6 failed, 100 passed"


def test_a_log_with_no_summary_states_no_count() -> None:
    assert job._suite_summary(["running eslint", "done"]) is None


def test_the_number_is_still_flattened() -> None:
    """A summary body is remote text and lands in this op's own render."""
    line = job.suite_line("1 failed\rStatus: success", 1, "failure")
    assert "\r" not in line, repr(line)
