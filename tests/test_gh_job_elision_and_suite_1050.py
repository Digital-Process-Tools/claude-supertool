"""#1050 — `:fail` elided the discriminating block behind a bare `...`.

Two defects, one reading of PR #1047's Windows red, and they compounded.

`_find_error_sections` joins the matched context windows with a bare ``...``
line. That string is indistinguishable from an ellipsis the *log* wrote — an
`AssertionError: ...` truncation, a pytest diff elision — so an absence the
**tool** produced renders exactly like content the log contains. On #1047 the
elided middle held ``fake : ok (no new errors)``, which was the whole
discriminator between three candidate causes, and recovering it cost a second
call with `:grep:`.

Second, the job's own terminal summary — ``6 failed, 7760 passed, 677 skipped``
— is one authoritative line that settles "four failed *legs* or four failed
*tests*" on sight, and neither `gh-job` nor `gh-pr` surfaced it. Reading
`4 failed` as four tests produced a confident wrong hypothesis (ordering /
shared state) instead of the true one (a uniform fixture failure).

Pinned here: every gap marker says it is the op's elision and how many lines it
covers, the suite summary is printed when the log states one, and neither is
invented when there is nothing to state.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


job = _load("presets/github/job.py", "github_job_1050")


# ---------------------------------------------------------------------------
# a gap the op cut must not render like a gap the log wrote
# ---------------------------------------------------------------------------

def _gap_lines(sections: list) -> list[str]:
    return [text for num, text in sections if num == -1]


def _two_gap_log() -> list[str]:
    """Two matched blocks with a wide, unmatched middle between them."""
    return (
        ["FAILED tests/test_a.py::test_one - AssertionError: x"]
        + [f"filler {i}" for i in range(40)]
        + ["FAILED tests/test_b.py::test_two - AssertionError: y"]
    )


def test_a_gap_marker_is_not_a_bare_ellipsis() -> None:
    """`...` alone is the log's own vocabulary — the op must not borrow it."""
    sections = job._find_error_sections(_two_gap_log(), ["FAILED"], 2)
    gaps = _gap_lines(sections)
    assert gaps, f"no gap produced by this fixture: {sections}"
    for gap in gaps:
        assert gap.strip() != "...", (
            "the elision renders as a bare '...', which is exactly what a "
            "truncated assertion looks like:\n" + repr(gap))


def test_a_gap_marker_says_how_many_lines_it_covers() -> None:
    """34 unshown lines and '...' are the same three characters. They are not."""
    lines = _two_gap_log()
    sections = job._find_error_sections(lines, ["FAILED"], 2)
    shown = {num for num, _ in sections if num > 0}
    hidden = len(lines) - len(shown)
    gaps = _gap_lines(sections)
    assert gaps, sections
    assert any(str(hidden) in gap for gap in gaps), (
        f"{hidden} lines were cut and no marker states the number: {gaps}")


def test_a_gap_marker_names_the_op_as_the_cause() -> None:
    """The reader has to know the log is intact and the *filter* cut."""
    sections = job._find_error_sections(_two_gap_log(), ["FAILED"], 2)
    gaps = _gap_lines(sections)
    assert gaps, sections
    assert all(re.search(r"not shown|elid|no error pattern", gap, re.I)
               for gap in gaps), (
        f"a gap marker that does not say who cut: {gaps}")


def test_contiguous_matches_produce_no_gap_marker() -> None:
    """A marker that fires with nothing between the blocks is a false alarm."""
    lines = ["FAILED one", "FAILED two", "FAILED three"]
    sections = job._find_error_sections(lines, ["FAILED"], 5)
    assert _gap_lines(sections) == [], (
        f"claimed an elision over an unbroken block: {sections}")


def test_the_trailing_gap_is_off_by_default() -> None:
    """The default render prints `## Tail` right under these sections.

    A 500-line log whose last match is at 400 got `... (99 lines elided …)`
    followed three lines later by lines 421-500 printed verbatim. The marker was
    false about eighty of the ninety-nine, and it was false in the direction
    that makes a reader stop looking.
    """
    lines = ["FAILED a"] + [f"x{i}" for i in range(60)]
    sections = job._find_error_sections(lines, ["FAILED"], 1)
    assert _gap_lines(sections) == [], (
        f"claimed a trailing elision the tail block then contradicts: "
        f"{_gap_lines(sections)}")


def test_the_trailing_gap_is_on_for_the_blocks_only_render() -> None:
    """`:fail` prints blocks and nothing else, so it can truthfully say it."""
    lines = ["FAILED a"] + [f"x{i}" for i in range(60)]
    sections = job._find_error_sections(lines, ["FAILED"], 1,
                                        trailing_gap=True)
    gaps = _gap_lines(sections)
    shown = {num for num, _ in sections if num > 0}
    assert gaps and str(len(lines) - len(shown)) in gaps[-1], gaps


def test_every_unshown_line_is_accounted_for_by_some_marker() -> None:
    """The counts across all gaps must equal what was actually withheld."""
    lines = (
        ["FAILED a"] + [f"x{i}" for i in range(20)]
        + ["FAILED b"] + [f"y{i}" for i in range(30)] + ["FAILED c"]
    )
    sections = job._find_error_sections(lines, ["FAILED"], 1,
                                        trailing_gap=True)
    shown = {num for num, _ in sections if num > 0}
    hidden = len(lines) - len(shown)
    counted = sum(int(n) for gap in _gap_lines(sections)
                  for n in re.findall(r"\d+", gap))
    assert counted == hidden, (
        f"markers account for {counted} lines, {hidden} were withheld: "
        f"{_gap_lines(sections)}")


# ---------------------------------------------------------------------------
# the one line that settles legs-vs-tests
# ---------------------------------------------------------------------------

# Line 533 of job 93033577461 — #1050's own job — copied verbatim. GitHub
# Actions is not a tty, so pytest writes no `=` rule around it. A first version
# of `_suite_summary` required those fences: it passed against a fixture built
# from the issue's prettified quote and matched nothing in any real CI log.
REAL_CI_SUMMARY = "6 failed, 7760 passed, 677 skipped, 2 warnings in 221.51s (0:03:41)"


def test_the_real_unfenced_ci_line_is_recognised() -> None:
    """The shape that actually appears in an Actions log, not a tty's."""
    found = job._suite_summary(["some earlier noise", REAL_CI_SUMMARY,
                                "##[error]Process completed with exit code 1."])
    assert found is not None, (
        "the line every Actions pytest log ends with was not recognised — "
        f"{REAL_CI_SUMMARY!r}")
    assert "6 failed" in found and "7760 passed" in found, found


def test_the_suite_summary_line_is_found() -> None:
    lines = [
        "installing deps",
        "=========== 6 failed, 7760 passed, 677 skipped in 221.51s ===========",
        "Error: Process completed with exit code 1.",
    ]
    found = job._suite_summary(lines)
    assert found is not None, "the authoritative count line was not recognised"
    assert "6 failed" in found and "7760 passed" in found, found


def test_no_tests_ran_states_no_counts() -> None:
    """`no tests ran in 0.12s` has a duration and no count. Not an answer."""
    assert job._suite_summary(["===== no tests ran in 0.12s ====="]) is None


def test_a_line_with_trailing_prose_is_not_a_summary() -> None:
    assert job._suite_summary(
        ["6 failed, 2 passed in 1.0s according to the previous job"]) is None


def test_the_suite_summary_prefers_the_last_one() -> None:
    """A re-run or a second pytest invocation writes two. The last one wins."""
    lines = [
        "==== 1 failed, 2 passed in 1.00s ====",
        "==== 6 failed, 7760 passed, 677 skipped in 221.51s ====",
    ]
    found = job._suite_summary(lines)
    assert found is not None and "6 failed" in found, found


def test_an_all_green_summary_is_still_reported() -> None:
    lines = ["===== 7760 passed, 677 skipped in 200.00s ====="]
    found = job._suite_summary(lines)
    assert found is not None and "7760 passed" in found, found


# ---------------------------------------------------------------------------
# review of PR #1064 — the fix reintroduced the defect it was fixing
# ---------------------------------------------------------------------------

def test_a_second_passing_run_does_not_hide_the_failing_one() -> None:
    """A `--lf` retry, a second suite step, tox — two summaries, one truth.

    Taking the trailing summary turned six real failures into `Suite: 7 passed`
    on the same job. That is the premise-correction failure #1050 exists to
    remove, reintroduced by #1050's own fix.
    """
    lines = [
        "6 failed, 100 passed in 200.00s",
        "re-running the last failures",
        "7 passed in 3.00s",
    ]
    found = job._suite_summary(lines)
    assert found is not None and "6 failed" in found, (
        f"a trailing green summary hid a failing one: {found!r}")


def test_with_no_failures_anywhere_the_last_summary_stands() -> None:
    lines = ["3 passed in 1.00s", "9 passed in 2.00s"]
    assert job._suite_summary(lines) == "9 passed"


def test_every_summary_is_enumerable() -> None:
    lines = ["6 failed, 100 passed in 200.00s", "7 passed in 3.00s"]
    assert len(job._suite_summaries(lines)) == 2


def test_an_indented_summary_is_not_the_jobs_own() -> None:
    """Captured subprocess output and `-s` reprint a nested run indented."""
    assert job._suite_summary(["    6 failed, 2 passed in 1.00s"]) is None


def test_a_warnings_only_summary_counts_no_tests_and_is_declined() -> None:
    """`2 warnings in 0.30s` under a header reading 'these count TESTS'."""
    assert job._suite_summary(["2 warnings in 0.30s"]) is None


def test_warnings_alongside_a_real_count_are_still_reported() -> None:
    found = job._suite_summary(["1 failed, 2 passed, 3 warnings in 1.00s"])
    assert found is not None and "1 failed" in found, found


def test_no_summary_is_never_invented() -> None:
    """Silence, not a zero: a log without pytest states no test count."""
    assert job._suite_summary(["cargo build", "error: could not compile"]) is None
    assert job._suite_summary([]) is None


def test_the_summary_is_not_matched_out_of_a_sentence() -> None:
    """Prose mentioning the words is not a terminal summary line."""
    assert job._suite_summary(
        ["the previous run had 6 failed tests, we think"]) is None


# ---------------------------------------------------------------------------
# gh-pr's tally names its unit, but only when there is something to misread
# ---------------------------------------------------------------------------

pr = _load("presets/github/pr.py", "github_pr_1050")


def test_a_failed_tally_says_the_count_is_legs() -> None:
    line = pr._leg_unit_line(["SUCCESS"] * 16 + ["FAILURE"] * 4)
    assert line, "a 4-failed tally states no unit at all"
    assert "LEG" in line.upper(), line
    assert "test" in line.lower(), (
        f"names legs but never says what it is NOT: {line}")


def test_a_green_tally_says_nothing() -> None:
    """A note on every render is one nobody reads on the render that matters."""
    assert pr._leg_unit_line(["SUCCESS"] * 20) == ""


def test_a_pending_tally_says_nothing() -> None:
    assert pr._leg_unit_line(["SUCCESS", "PENDING"]) == ""


def test_a_cancelled_leg_alone_says_nothing() -> None:
    """`4 cancelled` was never at risk of being read as four tests."""
    assert pr._leg_unit_line(["SUCCESS", "CANCELLED"]) == ""
