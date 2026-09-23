"""#2676: a `went_green` re-emitted for an unchanged green sha after an
intermediate `unknown` reading the `only=` filter swallowed downstream.

Observed live: one gh-branch poller emitted `went_green` for the same sha
five times across 51 minutes with no re-run explaining it (pid 51707,
`only=went_green,went_failed`, 2026-09-23). The state file the poller keeps
holds only the *last read*, never the last *answer this poller actually
reported*, so a same-sha recovery from `unknown` (which is not an answer --
runs on a concluded commit do not disappear, only the read of them can fail,
per #2333's own argument) compared against the raw prior reading and always
looked like a fresh transition.

`poll()` now tracks `answer_state`/`answer_sha` separately from
`branch_state`/`sha`: the latter is still the raw last read (unchanged, and
still what the #2333/#2436/#2537 direction guard and `no_run_streak` key off
of), the former only advances past a real answer -- never `UNKNOWN` in any
form, regardless of which of the three routes into it was taken (an
unreconciled tally with `has_unread_jobs=False`, the #2436 grace window
reaching its threshold, or the #2537 unread-jobs route). A read that comes
back matching the last real answer, on the same sha, is a recovery and does
not fire; the `unknown` reading in between still fires once, same as before.

Every "must not re-fire" case is paired with a positive control on the exact
same fixture shape, per this repo's own CLAUDE.md: a real GREEN -> FAILED ->
GREEN cycle on one sha (e.g. a re-run) is not a recovery and must still fire
both transitions -- the issue's own positive control.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest import mock

POLLER = Path(__file__).parent.parent / "presets" / "watch" / "sources" / "gh-branch" / "poller.py"
_spec = importlib.util.spec_from_file_location("gh_branch_poller_2676", POLLER)
assert _spec is not None and _spec.loader is not None
poller = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(poller)

SHA = "e1a6a4ac"


def _snap(state, sentence="", sha=SHA, repo="", error="", has_failed_leg=False,
          has_unread_jobs=False):
    return (state, sentence, sha, repo, error, has_failed_leg, has_unread_jobs)


def _ctx(ref="master"):
    return {"source": "gh-branch", "id": ref, "only": []}


def _established_green_state():
    return {"branch_state": poller.GREEN, "sha": SHA, "ref": "master",
             "lookup": poller.LOOKUP_OK, "no_run_streak": 0,
             "answer_state": poller.GREEN, "answer_sha": SHA}


def _poll_with(state, snap):
    with mock.patch.object(poller, "_snapshot", return_value=snap):
        return poller.poll(state, _ctx())


def test_unreconciled_tally_unknown_then_green_on_same_sha_emits_once() -> None:
    """The issue's own test plan, route 1: `branch.verdict()` returns UNKNOWN
    for an unreconciled tally with every job list read (`has_unread_jobs=
    False`), which the #2333/#2436/#2537 direction guard never touches
    (`raw_needs_guard` requires `has_unread_jobs`). The next poll reads GREEN
    again on the same sha -- a recovery, not a transition -- and must not
    re-fire `went_green`."""
    state = _established_green_state()

    events, state = _poll_with(
        state, _snap(poller.UNKNOWN, "UNKNOWN — an unreconciled tally",
                     has_unread_jobs=False))
    assert [e["event"] for e in events] == ["unknown"], events
    assert state["branch_state"] == poller.UNKNOWN, state
    # The last real answer must still read GREEN -- the recovery target.
    assert state["answer_state"] == poller.GREEN, state
    assert state["answer_sha"] == SHA, state

    events, state = _poll_with(state, _snap(poller.GREEN, "GREEN — all passed"))
    assert events == [], events
    assert state["branch_state"] == poller.GREEN, state
    assert state["answer_state"] == poller.GREEN, state


def test_unread_jobs_confirm_streak_unknown_then_green_emits_once() -> None:
    """Route 2 from the issue: the #2436/#2537 grace window reaching
    `UNKNOWN_CONFIRM_STREAK` on the unread-jobs route (`has_unread_jobs=
    True`). Two consecutive misses surface `unknown`; the next real GREEN
    read on the same sha is a recovery and must not re-fire."""
    state = _established_green_state()

    # First miss: within the grace window, absorbed, no event.
    events, state = _poll_with(
        state, _snap(poller.UNKNOWN, "UNKNOWN — job list did not come back",
                     has_unread_jobs=True))
    assert events == [], events
    assert state["branch_state"] == poller.GREEN, state

    # Second consecutive miss: the threshold, surfaces `unknown` for real.
    events, state = _poll_with(
        state, _snap(poller.UNKNOWN, "UNKNOWN — job list did not come back",
                     has_unread_jobs=True))
    assert [e["event"] for e in events] == ["unknown"], events
    assert state["branch_state"] == poller.UNKNOWN, state
    assert state["answer_state"] == poller.GREEN, state

    events, state = _poll_with(state, _snap(poller.GREEN, "GREEN — all passed"))
    assert events == [], events
    assert state["answer_state"] == poller.GREEN, state


def test_control_green_failed_green_on_one_sha_emits_twice() -> None:
    """The issue's own positive control: a real GREEN -> FAILED -> GREEN
    cycle on one sha (a re-run) is not a recovery from a non-answer -- FAILED
    is a genuine answer -- and must still fire both transitions."""
    state = _established_green_state()

    events, state = _poll_with(
        state, _snap(poller.NOT_GREEN, "NOT GREEN — a leg failed",
                     has_failed_leg=True))
    assert [e["event"] for e in events] == ["went_failed"], events
    assert state["answer_state"] == poller.NOT_GREEN_FAILED, state

    events, state = _poll_with(state, _snap(poller.GREEN, "GREEN — all passed"))
    assert [e["event"] for e in events] == ["went_green"], events
    assert state["answer_state"] == poller.GREEN, state


def test_a_new_sha_after_unknown_still_emits_even_if_state_matches() -> None:
    """A recovery is defined by sha too: GREEN, UNKNOWN, then GREEN on a
    DIFFERENT sha must still fire -- it is a brand new commit, not a return
    to the one already announced."""
    state = _established_green_state()

    events, state = _poll_with(
        state, _snap(poller.UNKNOWN, "UNKNOWN — an unreconciled tally",
                     has_unread_jobs=False))
    assert [e["event"] for e in events] == ["unknown"], events

    events, state = _poll_with(
        state, _snap(poller.GREEN, "GREEN — all passed", sha="deadbeef"))
    assert [e["event"] for e in events] == ["went_green"], events
    assert state["answer_sha"] == "deadbeef", state
