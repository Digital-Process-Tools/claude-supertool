"""#2362 -- a genuinely never-ran commit reads exactly like "still within
the ordinary window", forever, because ``no_run_verdict`` only ever knew
one threshold.

A squash merge into ``master`` produced a commit for which zero workflow
runs of any status ever existed -- verified ~20 minutes after the merge,
with path filters, the documented cancel-in-progress trade-off and rate
limiting all ruled out (#2362's own reproduction). ``no_run_verdict`` already
separates "inside the ~15min creation window" from "past it" (#585's own
grace, ``_GRACE``/``CHECK_CREATION_GRACE_SECS``) -- but past that window it
declines forever, at 20 minutes and at 20 hours alike: "whether any workflow
covers this ref is UNKNOWN" reads identically at both ages, so a commit that
will never see a run is never distinguished from one still plausibly waiting
on a slow listing.

``NO_RUN_STALE_SECS`` is the second, longer threshold: past it the ordinary
explanations (listing lag, a path filter) have had long enough to have
already resolved themselves, and what is left standing is escalated into
its own state, ``NO_RUN_STALE`` -- a different token from ``NO_RUN``, so a
caller comparing states (not scanning sentences) sees the difference too.

Every "must escalate" case here is paired with a "must not escalate yet"
case on the same function (CLAUDE.md's own positive-control rule) --
the anti-pattern this repository names as its own house defect is a check
that returns the same shape for "I looked and it's fine" and "I never
looked at all", and collapsing 20 minutes into 45 minutes is exactly that
shape one level up: both would print "UNKNOWN" and a reader could not tell
which one they were looking at.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location(
    "github_branch_2362", _ROOT / "presets" / "github" / "branch.py")
assert _spec is not None and _spec.loader is not None
branch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(branch)

_SHA = "abc1234567890"


# ---------------------------------------------------------------------------
# past the ordinary grace window but not yet stale: unchanged, plain NO_RUN
# ---------------------------------------------------------------------------

def test_past_grace_but_under_the_stale_threshold_stays_plain_no_run() -> None:
    """20 minutes old, zero runs -- past the ~15min grace, well short of the
    stale threshold. This is the exact age #2362's own reproduction observed,
    and at that age nothing here is entitled to escalate: a slow listing or a
    path filter is still a live explanation.
    """
    state, sentence = branch.no_run_verdict(_SHA, 20 * 60)
    assert state == branch.NO_RUN, (
        f"a commit only 20 minutes past its first run should not yet read "
        f"as the harder finding: {state}")
    assert state != branch.NO_RUN_STALE
    assert "UNKNOWN" in sentence


def test_just_under_the_stale_threshold_does_not_escalate() -> None:
    state, _sentence = branch.no_run_verdict(
        _SHA, branch.NO_RUN_STALE_SECS - 1)
    assert state == branch.NO_RUN


# ---------------------------------------------------------------------------
# past the stale threshold: an explicit, distinct finding
# ---------------------------------------------------------------------------

def test_past_the_stale_threshold_escalates_to_a_distinct_state() -> None:
    state, sentence = branch.no_run_verdict(
        _SHA, branch.NO_RUN_STALE_SECS + 1)
    assert state == branch.NO_RUN_STALE, (
        f"a commit this far past the ordinary window with still zero runs "
        f"must read as its own state, not as the same NO RUN a 20-minute-old "
        f"commit gets: {state}")
    assert state != branch.NO_RUN
    assert "2362" in sentence or "STALE" in sentence.upper()


def test_at_exactly_the_stale_threshold_does_not_escalate_yet() -> None:
    """The boundary is exclusive on the escalated side -- consistent with
    the ordinary grace check's own `<= grace` reading one branch up.
    """
    state, _sentence = branch.no_run_verdict(_SHA, branch.NO_RUN_STALE_SECS)
    assert state == branch.NO_RUN


def test_the_two_states_are_never_the_same_sentence() -> None:
    """The whole defect, in one assertion (mirrors #585's own regression
    test for the sibling absence-classifier)."""
    _state_a, sentence_a = branch.no_run_verdict(_SHA, 20 * 60)
    _state_b, sentence_b = branch.no_run_verdict(
        _SHA, branch.NO_RUN_STALE_SECS + 1)
    assert sentence_a != sentence_b


def test_unestablished_age_never_escalates() -> None:
    """An unknown commit age is its own, separate reading (`age_secs is
    None`) -- it must never be treated as "infinitely old" and escalated by
    accident.
    """
    state, sentence = branch.no_run_verdict(_SHA, None)
    assert state == branch.NO_RUN
    assert state != branch.NO_RUN_STALE
    assert "could not be established" in sentence
