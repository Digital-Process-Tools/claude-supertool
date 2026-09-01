"""`tests/_nested_pytest_verdict.py` (#2067, #2015).

Ported here so `test_repo_target_673.py` -- the GitHub-side twin of
`test_gl_repo_target_676.py`, running the identical nested-pytest probe
technique -- gets the same classification #2067 already gave the GitLab
side: a harness death (collection-time crash) must never render as a
product disagreement. Before this module existed as a shared import,
673 had no such classifier at all and would have reported occurrence
5 of #2015 as "the child pytest ran and disagreed", which it did not.
"""
from __future__ import annotations

import subprocess

import pytest

from _nested_pytest_verdict import assert_child_pytest_ran_and_passed


def test_a_child_that_crashes_at_collection_is_reported_as_a_harness_failure():
    """The positive control: a collection-time death (exit 2) must not be
    reported the same way as a real test failure."""
    result = subprocess.CompletedProcess(
        args=[], returncode=2, stdout="",
        stderr="Interrupted: 1 error during collection",
    )
    with pytest.raises(pytest.fail.Exception) as excinfo:
        assert_child_pytest_ran_and_passed(result)
    message = str(excinfo.value)
    assert "never produced a verdict" in message
    assert "exit 2" in message
    assert "ran and disagreed" not in message


def test_a_real_child_failure_is_still_reported_as_a_product_failure():
    """The negative control paired with the one above: a real product
    disagreement (exit 1) must still read as exactly that, not softened
    into "the harness failed" just because this file now also handles
    harness failures."""
    result = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="1 failed", stderr="",
    )
    with pytest.raises(pytest.fail.Exception) as excinfo:
        assert_child_pytest_ran_and_passed(result)
    message = str(excinfo.value)
    assert "ran and disagreed" in message
    assert "never produced a verdict" not in message


def test_a_clean_child_passes_silently():
    result = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    assert assert_child_pytest_ran_and_passed(result) is None


def test_extra_detail_is_appended_to_a_harness_failure_message():
    """#2015's payload: a temp-state snapshot must actually reach the
    failure message, or capturing it was pointless."""
    result = subprocess.CompletedProcess(
        args=[], returncode=2, stdout="", stderr="collection error",
    )
    with pytest.raises(pytest.fail.Exception) as excinfo:
        assert_child_pytest_ran_and_passed(
            result, extra_detail="-- temp state (before) --\n  tempdir: /x")
    assert "temp state (before)" in str(excinfo.value)
    assert "tempdir: /x" in str(excinfo.value)


def test_extra_detail_is_appended_to_a_product_failure_message_too():
    result = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="1 failed", stderr="",
    )
    with pytest.raises(pytest.fail.Exception) as excinfo:
        assert_child_pytest_ran_and_passed(result, extra_detail="marker-xyz")
    assert "marker-xyz" in str(excinfo.value)


def test_no_extra_detail_means_no_extra_lines_in_a_clean_pass():
    result = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    # Must not raise even when extra_detail is the default empty string.
    assert_child_pytest_ran_and_passed(result)
