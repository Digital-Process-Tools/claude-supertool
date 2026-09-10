"""`gh-branch`'s poller composing `NO_RUN_STALE` (#2362) with #2436's own
`UNKNOWN_CONFIRM_STREAK` direction guard, exercised through `poller.poll()`
end to end -- not through `branch.no_run_verdict()` alone, which
`tests/test_gh_branch_no_run_stale_2362.py` already pins.

The two changes touch the same file for different reasons: #2362 escalates a
raw `NO_RUN` reading to `NO_RUN_STALE` by age alone, with no memory of prior
polls; #2436 absorbs an isolated, self-recovering raw-empty read on a sha
this poller previously confirmed had runs, so a flaky listing does not
flap `unknown` on and off. `raw_is_no_run = branch_state in (NO_RUN,
NO_RUN_STALE)` is the one line that composes them -- this file is the
regression fence for that line and for the direction guard that reads it,
following the same fixture shape as `test_watch_gh_branch_flap_2436.py`.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest import mock

POLLER = Path(__file__).parent.parent / "presets" / "watch" / "sources" / "gh-branch" / "poller.py"
_spec = importlib.util.spec_from_file_location("gh_branch_poller_stale_composition_2362", POLLER)
assert _spec is not None and _spec.loader is not None
poller = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(poller)


def _snap(state, sentence="", sha="e1a6a4ac", repo="", error="", has_failed_leg=False):
    return (state, sentence, sha, repo, error, has_failed_leg)


def _ctx(ref="master"):
    return {"source": "gh-branch", "id": ref, "only": []}


def _stale_poll(state):
    with mock.patch.object(
            poller, "_snapshot",
            return_value=_snap(poller.NO_RUN_STALE,
                                "NO RUN — STALE — zero workflow runs on e1a6a4a")):
        return poller.poll(state, _ctx())


def test_a_single_isolated_stale_read_on_a_confirmed_sha_is_absorbed() -> None:
    """The must-not-fire half: a sha this poller previously confirmed had
    runs, one raw `NO_RUN_STALE` read -- exactly the shape #2436's guard
    exists to absorb, just with the #2362 token on the raw read instead of
    plain `NO_RUN`. Must fire nothing and leave the reported state
    unchanged, same as a plain-`NO_RUN` isolated blip does."""
    state = {"branch_state": poller.GREEN, "sha": "e1a6a4ac", "ref": "master",
              "lookup": poller.LOOKUP_OK}
    events, new_state = _stale_poll(state)
    assert events == [], events
    assert new_state["branch_state"] == poller.GREEN, new_state


def test_two_consecutive_stale_reads_surface_as_unknown_not_no_run_stale() -> None:
    """Positive control: the guard still counts a `NO_RUN_STALE` read
    toward its streak. Two consecutive reads on a confirmed sha surface
    `unknown` on the second, exactly as two consecutive plain-`NO_RUN`
    reads do -- the raw `NO_RUN_STALE` token must not be trusted early
    just because it is already the more severe reading."""
    state = {"branch_state": poller.GREEN, "sha": "e1a6a4ac", "ref": "master",
              "lookup": poller.LOOKUP_OK}
    events, state = _stale_poll(state)
    assert events == [], events

    events, state = _stale_poll(state)
    assert len(events) == 1, events
    assert events[0]["event"] == "unknown", events[0]["event"]
    assert state["branch_state"] == poller.UNKNOWN, state


def test_a_third_consecutive_stale_read_surfaces_the_real_no_run_stale() -> None:
    """The persistence promise, composed with #2362's own token: a THIRD
    consecutive raw `NO_RUN_STALE` read on the same confirmed sha is
    trusted, and the state that surfaces is the real `NO_RUN_STALE` --
    never downgraded back to plain `no_run` on the way out."""
    state = {"branch_state": poller.GREEN, "sha": "e1a6a4ac", "ref": "master",
              "lookup": poller.LOOKUP_OK}
    events, state = _stale_poll(state)
    assert events == [], events
    events, state = _stale_poll(state)
    assert events[0]["event"] == "unknown", events

    events, state = _stale_poll(state)
    assert len(events) == 1, events
    assert events[0]["event"] == "no_run_stale", events[0]["event"]
    assert state["branch_state"] == poller.NO_RUN_STALE, state


def test_a_fresh_sha_reading_no_run_stale_fires_immediately() -> None:
    """Must-fire twin: a sha this poller never confirmed runs on
    (`prev_confirmed_runs` False) has nothing to be graced against, so a
    raw `NO_RUN_STALE` read on it must surface `no_run_stale` on the very
    first poll -- the guard never applies to a genuinely fresh commit,
    with the #2362 token exactly as it already does for plain `no_run`."""
    state = {"branch_state": poller.GREEN, "sha": "old-sha", "ref": "master",
              "lookup": poller.LOOKUP_OK}
    with mock.patch.object(
            poller, "_snapshot",
            return_value=_snap(poller.NO_RUN_STALE,
                                "NO RUN — STALE — zero workflow runs on new1234",
                                sha="new-sha")):
        events, new_state = poller.poll(state, _ctx())
    assert len(events) == 1, events
    assert events[0]["event"] == "no_run_stale", events[0]["event"]
    assert new_state["branch_state"] == poller.NO_RUN_STALE
