"""Whether `windows-latest` has `awk` on PATH could not be confirmed from a
past session (#1221's own developer noted the same gap), so a whole leg's
`supertool-no-cut` regression coverage -- three files, positive controls
included -- could silently not run there, and a green Windows leg would read
identical to full coverage (#2505).

This file makes that silence loud on CI specifically, without touching the
correctly-quiet local case: a contributor's own machine with no `awk`
installed should keep skipping the three `test_jit_no_cut_*` files, not fail
their run over a tool they were never asked to install. `os.environ["CI"]`
is the split -- GitHub Actions sets it to `"true"` on every job by default
(no workflow-side opt-in needed), a local shell does not set it at all.

`assert_awk_present_on_ci` is the one function this pins, exercised twice:
against synthetic `is_ci`/`which_awk` inputs below (both directions --
`test_it_fires_...` is the positive control paired with
`test_it_does_not_fire_...`, the same MUST/MUST-NOT shape the three
`test_jit_no_cut_*` files already use, preserved rather than dropped in this
refactor), and once for real in `test_awk_is_actually_on_path_under_ci`
against this machine's own `os.environ` and `shutil.which` -- the assertion
that actually gates a CI leg.

Would `test_it_fires_when_ci_says_true_and_awk_is_absent` pass if
`assert_awk_present_on_ci` did nothing (returned `None` unconditionally)? No
-- that is exactly the case this test exists to catch, and it is the case
that used to be invisible: nothing before this file turned "CI job, no awk"
into a failure anywhere in this suite.
"""
from __future__ import annotations

import os
import shutil

import pytest


def assert_awk_present_on_ci(is_ci, which_awk):
    """Fail loudly when running on CI and `awk` is not on PATH.

    Local dev machines are deliberately exempt (`is_ci` false leaves this
    silent) -- the three `test_jit_no_cut_*` files already handle that case
    correctly via `needs_awk`, skipping only their own tests rather than
    failing the whole run over an optional local tool. This function is the
    other half: the environment CI is expected to provision it in.
    """
    if not is_ci:
        return
    assert which_awk is not None, (
        "awk is not on PATH on this CI runner. The supertool-no-cut "
        "regression suite (tests/test_jit_no_cut_word_boundary_2257.py, "
        "tests/test_jit_no_cut_span_stops_at_a_separator_1565.py, "
        "tests/test_jit_no_cut_path_name_1221.py) needs a real awk to "
        "reproduce what pre-tool-hook.sh actually compiles, and will "
        "otherwise silently skip its positive controls on this leg (#2505). "
        "See .github/workflows/tests.yml's windows-only "
        "'Ensure awk is on PATH' step.")


def test_it_fires_when_ci_says_true_and_awk_is_absent():
    with pytest.raises(AssertionError):
        assert_awk_present_on_ci(is_ci=True, which_awk=None)


def test_it_does_not_fire_when_ci_says_true_and_awk_is_present():
    assert assert_awk_present_on_ci(is_ci=True, which_awk="/usr/bin/awk") is None


def test_it_does_not_fire_when_not_running_under_ci():
    """A contributor's own machine with no awk stays silent here -- the
    per-file `needs_awk` skip in the three test_jit_no_cut_* files is what
    handles that case, and this function must not duplicate a failure on
    top of it."""
    assert assert_awk_present_on_ci(is_ci=False, which_awk=None) is None


def test_awk_is_actually_on_path_under_ci():
    """The real gate: this machine's own CI-ness and PATH, live.

    Skips (does not fail) off CI, for the same reason the function above
    stays silent there -- this is a statement about what CI must provision,
    not about a contributor's own machine."""
    is_ci = bool(os.environ.get("CI"))
    if not is_ci:
        pytest.skip("not running under CI (os.environ['CI'] unset) -- this "
                     "assertion is about what a CI runner must provision, "
                     "not about a local dev machine")
    assert_awk_present_on_ci(is_ci=is_ci, which_awk=shutil.which("awk"))
