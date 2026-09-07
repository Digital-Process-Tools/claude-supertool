"""Split pending vs failed inside NOT_GREEN, and sha-aware transitions (#2355).

`gh-branch`'s poller folded "nothing has concluded yet" and "a leg failed"
into the same coarse NOT_GREEN state, so a pending -> failed transition on
the *same* commit fired nothing: `branch_state != prev_state` was false on
both sides of it. Paired with the transition test never looking at `sha`, a
branch that moved to a brand-new commit while staying in the same coarse
category was equally silent. `master` sat red for 3h33m on 2026-09-07 with a
live watcher for exactly this reason -- see #2355's own event log.

The fix splits NOT_GREEN into two poller-level sub-states
(`NOT_GREEN_PENDING`, `NOT_GREEN_FAILED`), classified off the same substring
`branch.verdict()` already embeds in its sentence for the failed case
(`branch.NOT_GREEN_FAILED_MARKER`), and adds `sha` to the transition
condition so a same-state new-commit case emits too. Every must-not-fire
case below is paired with a must-fire twin, per this repo's own rule against
a broken harness passing by staying silent.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest import mock

POLLER = Path(__file__).parent.parent / "presets" / "watch" / "sources" / "gh-branch" / "poller.py"
_spec = importlib.util.spec_from_file_location("gh_branch_poller_2355", POLLER)
assert _spec is not None and _spec.loader is not None
poller = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(poller)

_PENDING_SENTENCE = ("NOT GREEN — nothing has failed, but tests have not "
                      "concluded on abc1234, so it is neither a pass nor a "
                      "fail. The commit is not cleared.")
_FAILED_SENTENCE = ("NOT GREEN — 3 legs on abc1234 did not pass, in tests. "
                     "Named below.")


def _snap(state, sentence="", sha="deadbeef", repo="", error=""):
    return (state, sentence, sha, repo, error)


def _ctx(ref="main"):
    return {"source": "gh-branch", "id": ref, "only": []}


# ---------------------------------------------------------------------------
# pending -> failed, same sha, same coarse NOT_GREEN: the silent transition
# ---------------------------------------------------------------------------

def test_pending_to_failed_same_sha_emits_went_failed() -> None:
    """The must-fire half: #2355's own incident, reproduced -- a leg that was
    still running concludes failed, on the SAME commit, so the coarse
    NOT_GREEN state never changed and the old transition test stayed
    silent."""
    state = {"branch_state": poller.NOT_GREEN_PENDING, "sha": "abc1234",
             "ref": "main", "lookup": poller.LOOKUP_OK}
    with mock.patch.object(
            poller, "_snapshot",
            return_value=_snap(poller.NOT_GREEN, _FAILED_SENTENCE, sha="abc1234")):
        events, new_state = poller.poll(state, _ctx())
    assert len(events) == 1, events
    assert events[0]["event"] == "went_failed"
    assert new_state["branch_state"] == poller.NOT_GREEN_FAILED


def test_pending_stays_pending_same_sha_fires_nothing() -> None:
    """The must-not-fire twin, paired directly above: a second poll of the
    same still-pending commit is not a transition."""
    state = {"branch_state": poller.NOT_GREEN_PENDING, "sha": "abc1234",
             "ref": "main", "lookup": poller.LOOKUP_OK}
    with mock.patch.object(
            poller, "_snapshot",
            return_value=_snap(poller.NOT_GREEN, _PENDING_SENTENCE, sha="abc1234")):
        events, _new_state = poller.poll(state, _ctx())
    assert events == [], events


# ---------------------------------------------------------------------------
# sha-aware transitions: a same-category new commit is not silent either
# ---------------------------------------------------------------------------

def test_same_state_new_sha_emits() -> None:
    """The must-fire half: a merge lands, the branch moves to a brand-new
    commit that is, for now, in the same coarse category (still pending) --
    the subject changed under an unchanged verdict, and a consumer holding
    the previous sentence has no way to learn that on its own."""
    state = {"branch_state": poller.NOT_GREEN_PENDING, "sha": "old1234",
             "ref": "main", "lookup": poller.LOOKUP_OK}
    with mock.patch.object(
            poller, "_snapshot",
            return_value=_snap(poller.NOT_GREEN, _PENDING_SENTENCE, sha="new5678")):
        events, new_state = poller.poll(state, _ctx())
    assert len(events) == 1, events
    assert events[0]["event"] == "went_not_green"
    assert new_state["sha"] == "new5678"


def test_same_state_same_sha_fires_nothing() -> None:
    """The must-not-fire twin, paired directly above."""
    state = {"branch_state": poller.GREEN, "sha": "same999",
             "ref": "main", "lookup": poller.LOOKUP_OK}
    with mock.patch.object(
            poller, "_snapshot",
            return_value=_snap(poller.GREEN, "GREEN — all clear", sha="same999")):
        events, _new_state = poller.poll(state, _ctx())
    assert events == [], events
