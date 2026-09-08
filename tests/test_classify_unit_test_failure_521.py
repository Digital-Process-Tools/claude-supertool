"""#521 -- the one slice of the CI-failure classifier this lane can honestly
build: the issue's own table names one row a RULE rather than a heuristic --
"unit test failure -> MANUAL, always" -- and says that line "should survive
any redesign of the rest of the table". Everything else in the table
(rector/prettier diff hunks, PHPStan, infra flakes) is written against a
GitLab-plus-PHP CI shape this repository does not run (#521's own comment,
2026-09-02), and the issue is explicit that a classifier needs a real corpus
before it is built at all.

The corpus this file is measured against -- six real failed CI runs on this
repository's own history, pulled live via `gh-run`/`gh-job:ID:fail`
(2026-09-09, run ids 34276615685, 34251408240, 34212429585, 34195914535,
34167935433, 34167236734) -- turned up ONLY unit-test-shaped failures: a
stale docs-size assertion, a concurrency-serialization flake, and a
cross-cutting guard (`test_handrolled_path_env_guard_1151`) tripped by a
new test file. None of the issue's other rows (rector, PHPStan, infra
timeout) occurred even once in this sample, which is itself the measurement
this lane was asked to make: on a GitHub-Actions/pytest repo, the table's
non-unit-test rows may not be where the mass of real failures lives at all.
`docs/operations/ci-failure-classification.md` writes this up in full.

`classify_unit_test_failure` recognises pytest's own short-summary marker --
a line matching `FAILED <nodeid>` in either pytest's native `::`-separated
form or the dotted form this repo's `.github/scripts/junit_summary.py`
re-emits -- and returns `"MANUAL"` when found, `None` otherwise. `None` is
NOT "not a unit test failure"; it is "this one rule did not fire", the
third state this codebase's own defect class (CLAUDE.md, "The defect this
codebase keeps having") asks every checker to carry.
"""
from __future__ import annotations

import _job_argv


class TestFiresOnRealPytestFailureText:
    """MUST fire -- three real `gh-job:ID:fail` bodies pulled from this
    repository's own CI history, one per distinct shape of failure
    encountered in the sample.
    """

    def test_short_summary_colon_form(self) -> None:
        text = (
            "=========================== short test summary info ============================\n"
            "FAILED tests/test_render_size_claims_1877.py::test_every_documented_render_size_matches_the_render[docs/operations/index.md:ops:full] - AssertionError: docs/operations/index.md says ops:full is ~82.58KB; this checkout renders 82.81KB (82,805 bytes).\n"
            "assert 0.22500000000000853 < 0.1\n"
        )
        assert _job_argv.classify_unit_test_failure(text) == "MANUAL"

    def test_junit_summary_dotted_form(self) -> None:
        text = (
            "1 failing test(s), full messages from junit.xml:\n"
            "FAILED tests.test_go_warmup_lock_2331.test_two_racing_calls_do_not_overlap\n"
            "    AssertionError: more than one caller was inside fn() at the same real moment -- serialization did not hold (max concurrently active: 2)\n"
        )
        assert _job_argv.classify_unit_test_failure(text) == "MANUAL"

    def test_guard_test_tripped_by_a_different_file(self) -> None:
        text = (
            "FAILED tests/test_handrolled_path_env_guard_1151.py::test_an_env_the_scanner_cannot_read_is_declared_rather_than_assumed_clean - AssertionError: the scanner reached a sys.executable spawn whose env= it cannot evaluate.\n"
        )
        assert _job_argv.classify_unit_test_failure(text) == "MANUAL"


class TestDoesNotFireOnNonTestText:
    """MUST NOT fire -- paired with the positive cases above in the same
    fixture family, so a broken matcher that fires on everything (or a
    harness that sees nothing) cannot pass this file the same way a
    correctly-scoped one does.
    """

    def test_plain_infra_looking_log_has_no_verdict(self) -> None:
        text = (
            "##[error]The runner has received a shutdown signal.\n"
            "Error: The operation was canceled.\n"
        )
        assert _job_argv.classify_unit_test_failure(text) is None

    def test_the_word_failed_alone_does_not_fire(self) -> None:
        """A step name or prose sentence containing the bare word FAILED
        with no pytest node id after it must not be mistaken for the marker
        -- e.g. a job-table row this op itself renders elsewhere.
        """
        text = "Job pytest (ubuntu-latest, 3.12) -- failure\nstep: Run tests\n"
        assert _job_argv.classify_unit_test_failure(text) is None

    def test_empty_text_has_no_verdict(self) -> None:
        assert _job_argv.classify_unit_test_failure("") is None
