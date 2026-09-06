"""Direction guard: a poll of a SHA that already has confirmed runs must
never regress to NO_RUN (#2333).

Observed live: `went_green` -> `no_run` -> `went_green`, all on the same
SHA, 36 seconds apart, while `gh-branch` run cold seconds after the middle
event showed four concluded, all-passing runs on that exact commit. Runs on
a concluded commit do not disappear -- only the read of them can fail -- so
a poll claiming zero runs on a SHA this poller already saw runs on is a
fetch that did not answer, not a fact about the world, and must render
UNKNOWN rather than NO_RUN.

The must-fire twin is required by this repository's own defect class
(CLAUDE.md): a "must not fire" assertion passes when the poller emits
nothing at all, so a fresh SHA that legitimately has zero runs must still
fire `no_run` for real.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest import mock

POLLER = Path(__file__).parent.parent / "presets" / "watch" / "sources" / "gh-branch" / "poller.py"
_spec = importlib.util.spec_from_file_location("gh_branch_poller_2333", POLLER)
assert _spec is not None and _spec.loader is not None
poller = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(poller)


def _snap(state, sentence="", sha="deadbeef", repo="", error=""):
    return (state, sentence, sha, repo, error)


def _ctx(ref="main"):
    return {"source": "gh-branch", "id": ref, "only": []}


def test_no_run_on_a_sha_that_already_went_green_is_read_as_unknown() -> None:
    """The must-not-fire half: same SHA, confirmed green, then a listing
    claiming zero runs -- must not render as `no_run`."""
    state = {"branch_state": poller.GREEN, "sha": "e1a6a4ac", "ref": "main",
             "lookup": poller.LOOKUP_OK}
    with mock.patch.object(
            poller, "_snapshot",
            return_value=_snap(poller.NO_RUN,
                               "NO RUN — zero workflow runs on e1a6a4a",
                               sha="e1a6a4ac")):
        events, new_state = poller.poll(state, _ctx())
    assert len(events) == 1, events
    assert events[0]["event"] == "unknown", events[0]["event"]
    assert events[0]["event"] != "no_run"
    assert new_state["branch_state"] == poller.UNKNOWN


def test_no_run_on_a_sha_that_already_went_not_green_is_also_read_as_unknown() -> None:
    """The same guard applies coming from NOT_GREEN, not only from GREEN --
    either one means runs were confirmed to exist on this SHA."""
    state = {"branch_state": poller.NOT_GREEN, "sha": "bbbb", "ref": "main",
             "lookup": poller.LOOKUP_OK}
    with mock.patch.object(
            poller, "_snapshot",
            return_value=_snap(poller.NO_RUN,
                               "NO RUN — zero workflow runs on bbbbbbb",
                               sha="bbbb")):
        events, new_state = poller.poll(state, _ctx())
    assert len(events) == 1, events
    assert events[0]["event"] == "unknown", events[0]["event"]


def test_no_run_on_a_fresh_sha_that_never_went_green_still_fires_no_run() -> None:
    """The must-fire positive control: a genuinely fresh commit with zero
    runs must still surface as `no_run` -- the guard is keyed on the SHA
    matching, not on suppressing the event altogether."""
    state = {"branch_state": poller.GREEN, "sha": "old-sha", "ref": "main",
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


def test_no_run_on_first_ever_poll_of_a_sha_fires_no_run() -> None:
    """A cold poller (no prior state at all) with zero runs on the head has
    nothing to regress from, so the guard must not swallow this either --
    the gap the issue itself names as the direction-guard's known limit."""
    with mock.patch.object(
            poller, "_snapshot",
            return_value=_snap(poller.NO_RUN,
                               "NO RUN — zero workflow runs on cccccc")):
        events, new_state = poller.poll({}, _ctx())
    assert len(events) == 1, events
    assert events[0]["event"] == "no_run", events[0]["event"]


def test_unknown_reading_still_counts_as_confirmed_runs_for_the_guard() -> None:
    """`UNKNOWN` from unread job lists still implies runs exist on the SHA
    (the verdict only reaches that branch when `selected` is non-empty), so
    it must arm the guard exactly like GREEN/NOT_GREEN -- the guard applies
    (`branch_state` is downgraded from `NO_RUN` to `UNKNOWN`, never allowed
    to read as `no_run`), and since the poller's belief does not actually
    change (`UNKNOWN` -> `UNKNOWN`), no event fires -- the same "unchanged
    state" rule that already governs two consecutive GREEN polls."""
    state = {"branch_state": poller.UNKNOWN, "sha": "ccc", "ref": "main",
             "lookup": poller.LOOKUP_OK}
    with mock.patch.object(
            poller, "_snapshot",
            return_value=_snap(poller.NO_RUN,
                               "NO RUN — zero workflow runs on ccccccc",
                               sha="ccc")):
        events, new_state = poller.poll(state, _ctx())
    assert events == [], events
    assert new_state["branch_state"] == poller.UNKNOWN, new_state
    assert "NO RUN" not in new_state["branch_state"]


def test_the_uncertain_state_survives_a_second_no_run_poll_without_reflaring() -> None:
    """Once the guard has downgraded one `no_run` reading to `unknown` on a
    SHA, an immediate second `no_run` reading on the same SHA must not fire
    a second, duplicate event -- `unknown` is what the poller now believes,
    so the second poll is an unchanged state, not a fresh transition."""
    state = {"branch_state": poller.UNKNOWN, "sha": "e1a6a4ac", "ref": "main",
             "lookup": poller.LOOKUP_OK}
    with mock.patch.object(
            poller, "_snapshot",
            return_value=_snap(poller.NO_RUN,
                               "NO RUN — zero workflow runs on e1a6a4a",
                               sha="e1a6a4ac")):
        events, _new_state = poller.poll(state, _ctx())
    assert events == [], events
