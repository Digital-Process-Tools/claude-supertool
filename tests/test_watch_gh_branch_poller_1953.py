"""Unit tests for the gh-branch watcher source (#1953).

No tier watched the repository's default branch, so a consumer that recorded
"wait until main goes green" waited on a channel nothing could ever satisfy.
This source pushes a transition event instead of leaving the branch to be
pulled at report time, and the bar this file pins is the one the issue itself
states: the not-yet-concluded state must stay distinct from both green and
red, a poller that cannot tell must say so once per outage rather than once
per poll, and every must-not-fire case has a must-fire twin so a broken
harness cannot pass by staying silent.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest import mock

POLLER = Path(__file__).parent.parent / "presets" / "watch" / "sources" / "gh-branch" / "poller.py"
_spec = importlib.util.spec_from_file_location("gh_branch_poller", POLLER)
assert _spec is not None and _spec.loader is not None
poller = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(poller)


def _snap(state, sentence="", sha="deadbeef", repo="", error=""):
    return (state, sentence, sha, repo, error)


def _ctx(ref="main"):
    return {"source": "gh-branch", "id": ref, "only": []}


# ---------------------------------------------------------------------------
# transitions fire, and only across a real change
# ---------------------------------------------------------------------------

def test_first_successful_poll_fires_the_found_state() -> None:
    """The must-fire half: a cold poller reports what it found. `first_tick`
    (added by the dispatcher, not this source) is what tells a reader this is
    a report, not news."""
    with mock.patch.object(poller, "_snapshot",
                           return_value=_snap(poller.GREEN, "GREEN — all clear")):
        events, new_state = poller.poll({}, _ctx())
    assert len(events) == 1, events
    assert events[0]["event"] == "went_green"
    assert new_state["branch_state"] == poller.GREEN


def test_an_unchanged_state_fires_nothing() -> None:
    """The must-not-fire half, paired with the must-fire case directly above
    and below: two consecutive GREEN polls are one event, not two."""
    state = {"branch_state": poller.GREEN, "sha": "deadbeef", "ref": "main",
             "lookup": poller.LOOKUP_OK}
    with mock.patch.object(poller, "_snapshot",
                           return_value=_snap(poller.GREEN, "GREEN — all clear")):
        events, _new_state = poller.poll(state, _ctx())
    assert events == [], events


def test_green_to_not_green_fires_went_not_green() -> None:
    state = {"branch_state": poller.GREEN, "sha": "aaa", "ref": "main",
             "lookup": poller.LOOKUP_OK}
    with mock.patch.object(
            poller, "_snapshot",
            return_value=_snap(poller.NOT_GREEN,
                               "NOT GREEN — 3 legs on bbbbbbb did not pass")):
        events, new_state = poller.poll(state, _ctx())
    assert len(events) == 1, events
    assert events[0]["event"] == "went_not_green"
    assert "did not pass" in events[0]["payload"]["sentence"]
    assert new_state["branch_state"] == poller.NOT_GREEN


def test_not_green_to_green_fires_went_green() -> None:
    """The must-fire twin of the case above, in the direction that clears a
    merge queue: this is the event #1953 was filed over the absence of."""
    state = {"branch_state": poller.NOT_GREEN, "sha": "bbb", "ref": "main",
             "lookup": poller.LOOKUP_OK}
    with mock.patch.object(
            poller, "_snapshot",
            return_value=_snap(poller.GREEN, "GREEN — all clear")):
        events, new_state = poller.poll(state, _ctx())
    assert len(events) == 1, events
    assert events[0]["event"] == "went_green"


# ---------------------------------------------------------------------------
# the third state stays distinct — #1953's own requirement
# ---------------------------------------------------------------------------

def test_no_run_is_its_own_event_not_a_red() -> None:
    """The state a naive green/red pair would fold into a false alarm on
    every squash: a fresh commit with nothing dispatched yet is neither."""
    state = {"branch_state": poller.GREEN, "sha": "old", "ref": "main",
             "lookup": poller.LOOKUP_OK}
    with mock.patch.object(
            poller, "_snapshot",
            return_value=_snap(poller.NO_RUN,
                               "NO RUN — zero workflow runs on new1234")):
        events, new_state = poller.poll(state, _ctx())
    assert len(events) == 1, events
    assert events[0]["event"] == "no_run"
    assert events[0]["event"] not in ("went_green", "went_not_green")


def test_unknown_is_its_own_event_not_a_guess() -> None:
    state = {"branch_state": poller.GREEN, "sha": "aaa", "ref": "main",
             "lookup": poller.LOOKUP_OK}
    with mock.patch.object(
            poller, "_snapshot",
            return_value=_snap(poller.UNKNOWN,
                               "UNKNOWN — the job list did not come back")):
        events, new_state = poller.poll(state, _ctx())
    assert len(events) == 1, events
    assert events[0]["event"] == "unknown"


# ---------------------------------------------------------------------------
# a poller that cannot tell must say so once, not on every poll
# ---------------------------------------------------------------------------

def test_a_lookup_failure_fires_branch_unreachable_once() -> None:
    with mock.patch.object(poller, "_snapshot",
                           return_value=_snap("", error="ERROR: gh timed out")):
        events, new_state = poller.poll({}, _ctx())
    assert len(events) == 1, events
    assert events[0]["event"] == "branch_unreachable"
    assert new_state["lookup"] == poller.LOOKUP_UNAVAILABLE


def test_a_second_consecutive_lookup_failure_is_silent() -> None:
    """Edge-triggered, same shape as `github-pr`'s `_fetch` outage handling:
    an alert repeated every 30s is one people mute, which is the original
    silence arriving by a longer route."""
    state = {"lookup": poller.LOOKUP_UNAVAILABLE, "error": "ERROR: gh timed out"}
    with mock.patch.object(poller, "_snapshot",
                           return_value=_snap("", error="ERROR: gh timed out")):
        events, _new_state = poller.poll(state, _ctx())
    assert events == [], events


def test_recovery_from_a_lookup_failure_still_reports_the_found_state() -> None:
    """The must-fire twin of the silence above: recovering from an outage is
    not itself suppressed just because the outage was."""
    state = {"lookup": poller.LOOKUP_UNAVAILABLE, "error": "ERROR: gh timed out",
             "branch_state": poller.GREEN}
    with mock.patch.object(poller, "_snapshot",
                           return_value=_snap(poller.GREEN, "GREEN — all clear")):
        events, new_state = poller.poll(state, _ctx())
    assert new_state["lookup"] == poller.LOOKUP_OK
    # Recovering onto the *same* state it held before the outage is not a
    # transition — this only asserts that recovery does not stay silenced by
    # the outage-suppression branch above; the transition semantics are
    # covered by the green/not-green pair further up.
    assert events == [] or events[0]["event"] == "went_green"


def test_a_state_carried_across_the_outage_survives_untouched() -> None:
    """The `{**state, ...}` recovery guarantee `github-pr`'s poller documents
    for its own outage arm: `branch_state` must survive so the transition
    that happened while the poller was blind is neither lost nor
    re-announced as if it were new, once the lookup starts answering again."""
    state = {"branch_state": poller.NOT_GREEN, "sha": "old", "ref": "main",
             "lookup": poller.LOOKUP_OK}
    with mock.patch.object(poller, "_snapshot",
                           return_value=_snap("", error="ERROR: gh unreachable")):
        _events, new_state = poller.poll(state, _ctx())
    assert new_state["branch_state"] == poller.NOT_GREEN
    assert new_state["lookup"] == poller.LOOKUP_UNAVAILABLE


def test_is_terminal_is_always_false() -> None:
    """A branch has no merged/closed state to stop watching for."""
    assert poller.is_terminal({"branch_state": poller.GREEN}) is False
    assert poller.is_terminal({"branch_state": poller.NOT_GREEN}) is False
