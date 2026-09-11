"""A confirmed sha must not re-announce `unknown` on every isolated,
self-recovering blip in the run-list endpoint (#2436).

Observed live: `gh-branch`'s poller on `master` flapped `went_green` ->
`unknown` -> `went_green` six times in 32 minutes on one unchanged, already
concluded, already-green commit. Every `unknown` was individually correct
per #2333's own direction guard -- a previous poll confirmed runs on this
sha, this poll's run list came back empty, so the guard downgraded `no_run`
to `unknown` rather than trusting it -- but each incident was an isolated,
single-poll blip that recovered on the very next 30s poll (~39s later),
and the guard re-armed itself instantly on every recovery, so it announced
(and un-announced) the identical anomaly six separate times.

`UNKNOWN_CONFIRM_STREAK` (poller.py) raises the bar from #2333's original
1 consecutive empty read to 2: fewer than that is discarded as if the poll
never happened, so an isolated blip that recovers on the very next poll
produces zero events. The must-fire twin (CLAUDE.md's own defect class)
is that a *persistent* absence -- one that does NOT recover -- still
surfaces, first as `unknown` once the threshold is reached and then, if it
keeps not recovering, as the real `no_run` -- so this is a cadence fix,
never a suppression of the finding #2333/#2355 exist to make.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest import mock

POLLER = Path(__file__).parent.parent / "presets" / "watch" / "sources" / "gh-branch" / "poller.py"
_spec = importlib.util.spec_from_file_location("gh_branch_poller_2436", POLLER)
assert _spec is not None and _spec.loader is not None
poller = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(poller)


def _snap(state, sentence="", sha="e1a6a4ac", repo="", error="", has_failed_leg=False):
    return (state, sentence, sha, repo, error, has_failed_leg)


def _ctx(ref="master"):
    return {"source": "gh-branch", "id": ref, "only": []}


def _empty_poll(state):
    with mock.patch.object(
            poller, "_snapshot",
            return_value=_snap(poller.NO_RUN,
                                "NO RUN — zero workflow runs on e1a6a4a")):
        return poller.poll(state, _ctx())


def test_a_single_isolated_empty_fetch_fires_no_event_at_all() -> None:
    """The must-not-fire half, and the exact shape of the six observed
    incidents: one confirmed GREEN sha, one raw-empty fetch on that same
    sha. Under the pre-#2436 single-shot guard this fired `unknown`
    immediately -- the six-flaps-in-32-minutes bug. It must now fire
    nothing at all, and the reported state must stay GREEN, not UNKNOWN,
    so a consumer polling state directly sees no anomaly either."""
    state = {"branch_state": poller.GREEN, "sha": "e1a6a4ac", "ref": "master",
              "lookup": poller.LOOKUP_OK}
    events, new_state = _empty_poll(state)
    assert events == [], events
    assert new_state["branch_state"] == poller.GREEN, new_state


def test_isolated_blip_then_recovery_never_touches_the_channel() -> None:
    """The full observed cycle: confirmed GREEN, one empty poll (absorbed),
    then a real run list again on the same sha. Zero events across the
    whole cycle -- the six pairs of `unknown`/`went_green` this issue was
    filed over must both disappear, not just the first half."""
    state = {"branch_state": poller.GREEN, "sha": "e1a6a4ac", "ref": "master",
              "lookup": poller.LOOKUP_OK}
    events, state = _empty_poll(state)
    assert events == [], events

    with mock.patch.object(
            poller, "_snapshot",
            return_value=_snap(poller.GREEN,
                                "GREEN — every run on e1a6a4a passed")):
        events, state = poller.poll(state, _ctx())
    assert events == [], events
    assert state["branch_state"] == poller.GREEN, state


def test_two_consecutive_empty_fetches_surface_as_unknown() -> None:
    """Positive control: a genuine, non-recovering absence still gets
    reported. Two consecutive raw-empty fetches on the same confirmed sha
    -- no recovery in between -- must surface `unknown` on the second one,
    exactly the finding #2333/#2355 exist to make."""
    state = {"branch_state": poller.GREEN, "sha": "e1a6a4ac", "ref": "master",
              "lookup": poller.LOOKUP_OK}
    events, state = _empty_poll(state)
    assert events == [], events

    events, state = _empty_poll(state)
    assert len(events) == 1, events
    assert events[0]["event"] == "unknown", events[0]["event"]
    assert state["branch_state"] == poller.UNKNOWN, state


def test_a_third_consecutive_empty_fetch_surfaces_the_real_no_run() -> None:
    """The persistence promise, one poll later than #2333's original: a
    THIRD consecutive raw-empty read on the same sha -- the anomaly has now
    outlasted the grace window AND the first `unknown` announcement -- is
    trusted as a genuine, sustained emptiness (e.g. GitHub's own
    run-retention purge) and fires the real `no_run`, not `unknown`
    forever."""
    state = {"branch_state": poller.GREEN, "sha": "e1a6a4ac", "ref": "master",
              "lookup": poller.LOOKUP_OK}
    events, state = _empty_poll(state)
    assert events == [], events
    events, state = _empty_poll(state)
    assert events[0]["event"] == "unknown", events

    events, state = _empty_poll(state)
    assert len(events) == 1, events
    assert events[0]["event"] == "no_run", events[0]["event"]
    assert state["branch_state"] == poller.NO_RUN, state


def test_a_fresh_sha_with_zero_runs_still_fires_no_run_immediately() -> None:
    """Must-fire twin, unaffected by the grace window: a brand-new commit
    that genuinely has zero runs has nothing to be graced against
    (`prev_confirmed_runs` is False) and must still surface `no_run` on the
    very first poll -- this repository's own named positive-control
    requirement (CLAUDE.md), re-checked against this exact fix."""
    state = {"branch_state": poller.GREEN, "sha": "old-sha", "ref": "master",
              "lookup": poller.LOOKUP_OK}
    with mock.patch.object(
            poller, "_snapshot",
            return_value=_snap(poller.NO_RUN,
                                "NO RUN — zero workflow runs on new1234",
                                sha="new-sha")):
        events, new_state = poller.poll(state, _ctx())
    assert len(events) == 1, events
    assert events[0]["event"] == "no_run", events[0]["event"]
    assert new_state["branch_state"] == poller.NO_RUN
