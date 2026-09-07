"""Split pending vs failed inside NOT_GREEN, and sha-aware transitions (#2355).

`gh-branch`'s poller folded "nothing has concluded yet" and "a leg failed"
into the same coarse NOT_GREEN state, so a pending -> failed transition on
the *same* commit fired nothing: `branch_state != prev_state` was false on
both sides of it. Paired with the transition test never looking at `sha`, a
branch that moved to a brand-new commit while staying in the same coarse
category was equally silent. `master` sat red for 3h33m on 2026-09-07 with a
live watcher for exactly this reason -- see #2355's own event log.

The fix splits NOT_GREEN into two poller-level sub-states
(`NOT_GREEN_PENDING`, `NOT_GREEN_FAILED`), classified off `_snapshot`'s own
`has_failed_leg` boolean (`bool(branch._red_workflows(selected, legs))`) --
never a substring search over `verdict()`'s rendered sentence. A first cut of
this fix used exactly that substring search
(`branch.NOT_GREEN_FAILED_MARKER in sentence`) and both reviewers spawned
against the committed diff independently found the same hole: `verdict()`'s
pending sentence interpolates a workflow's own `name:` field
(`_names(moving)`/`_names(missing)`), which GitHub lets a repo author spell
however they like, so a workflow literally named "did not pass" would have
forged a false NOT_GREEN_FAILED reading on a genuinely pending commit --
`test_a_workflow_named_after_the_old_marker_text_does_not_forge_failed`
below pins that directly. Also adds `sha` to the transition condition so a
same-state new-commit case emits too. Every must-not-fire case below is
paired with a must-fire twin, per this repo's own rule against a broken
harness passing by staying silent.
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
# The crafted-name sentence: a workflow literally named after the substring a
# first cut of this fix used to classify on. Shaped exactly like a real
# `verdict()` pending sentence (`_names(moving)` interpolated with backticks)
# -- nothing has failed here, the branch is only waiting on this one
# workflow, whose *name* happens to be the phrase.
_FORGED_PENDING_SENTENCE = ("NOT GREEN — nothing has failed, but `did not "
                             "pass` has not concluded on abc1234, so it is "
                             "neither a pass nor a fail. The commit is not "
                             "cleared.")


def _snap(state, sentence="", sha="deadbeef", repo="", error="", has_failed_leg=False):
    return (state, sentence, sha, repo, error, has_failed_leg)


def _ctx(ref="main"):
    return {"source": "gh-branch", "id": ref, "only": []}


# ---------------------------------------------------------------------------
# pending -> failed, same sha, same coarse NOT_GREEN: the silent transition
# ---------------------------------------------------------------------------

def test_pending_to_failed_same_sha_emits_went_failed() -> None:
    """The must-fire half: #2355's own incident, reproduced -- a leg that was
    still running concludes failed, on the SAME commit, so the coarse
    NOT_GREEN state never changed and the old transition test stayed
    silent. `has_failed_leg=True` is `_snapshot`'s own structural finding."""
    state = {"branch_state": poller.NOT_GREEN_PENDING, "sha": "abc1234",
             "ref": "main", "lookup": poller.LOOKUP_OK}
    with mock.patch.object(
            poller, "_snapshot",
            return_value=_snap(poller.NOT_GREEN, _FAILED_SENTENCE, sha="abc1234",
                                has_failed_leg=True)):
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
            return_value=_snap(poller.NOT_GREEN, _PENDING_SENTENCE, sha="abc1234",
                                has_failed_leg=False)):
        events, _new_state = poller.poll(state, _ctx())
    assert events == [], events


# ---------------------------------------------------------------------------
# the classifier reads structured data, never the rendered sentence
# ---------------------------------------------------------------------------

def test_a_workflow_named_after_the_old_marker_text_does_not_forge_failed() -> None:
    """A workflow whose GitHub-author-chosen name happens to contain the
    phrase a first cut of this classifier scanned for must NOT flip a
    genuinely pending commit to `went_failed`. `has_failed_leg=False` is what
    `_snapshot` would actually report here -- nothing has failed, the
    forged-looking substring only exists inside the workflow's own name,
    which `_snapshot` never reads back out of the sentence."""
    state = {"branch_state": poller.NOT_GREEN_PENDING, "sha": "abc1234",
             "ref": "main", "lookup": poller.LOOKUP_OK}
    with mock.patch.object(
            poller, "_snapshot",
            return_value=_snap(poller.NOT_GREEN, _FORGED_PENDING_SENTENCE,
                                sha="def5678", has_failed_leg=False)):
        events, new_state = poller.poll(state, _ctx())
    assert len(events) == 1, events
    assert events[0]["event"] == "went_not_green"
    assert events[0]["event"] != "went_failed"
    assert new_state["branch_state"] == poller.NOT_GREEN_PENDING


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
            return_value=_snap(poller.NOT_GREEN, _PENDING_SENTENCE, sha="new5678",
                                has_failed_leg=False)):
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


# ---------------------------------------------------------------------------
# the source of truth itself: `branch._red_workflows` against a real
# `verdict()` call, not a mock -- the reviewer's own reproduction, pinned
# ---------------------------------------------------------------------------

def test_a_workflow_named_after_the_marker_reads_as_pending_at_the_source() -> None:
    """`branch.verdict()`'s own pending sentence, built with a workflow
    genuinely named "did not pass" and no failed leg, still contains that
    substring (proving the old text-scan approach really would have
    misfired) -- but `branch._red_workflows`, the structural check `poll()`
    now reads instead, correctly reports no failure at all."""
    branch = poller.branch
    selected = {"did not pass": {"status": "in_progress", "conclusion": None}}
    legs = {"did not pass": ["IN_PROGRESS"]}
    state, sentence = branch.verdict(selected, legs, set(), "abc1234", 4000,
                                      scope="")
    assert state == branch.NOT_GREEN, (state, sentence)
    # The forgery this fix closes: the old marker text really is in here.
    assert "did not pass" in sentence, sentence
    # And the structural check `poll()` now uses is not fooled by it.
    assert branch._red_workflows(selected, legs) == []
