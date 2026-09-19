"""`approved` and `retargeted` events for the `gitlab-mr` watch source (#2645).

Before this, the source emitted pipeline and comment transitions but stayed
silent on an MR reaching its approval rule or being retargeted onto a
different branch -- both change what a maintainer does next, and both were
only discoverable by re-reading the MR by hand.

Mirrors `tests/test_watch_gitlab_mr_poller.py`'s own fixture shape (a fresh
`importlib` load of `poller.py`, `_glab_api` stubbed by an autouse fixture)
rather than importing its private `_mr` helper, which has no `target_branch`
or approvals knob and is not this file's to extend.

`approved`'s cost gate went through a self-review round (#2645 review): the
first version paid the separate approvals request on *every* poll for as
long as `detailed_merge_status` stayed `"not_approved"`, not once per
transition as the docs and commit message claimed. `not_approved` is
answered for free instead -- it is the same fact `_fetch_approvals` would
return -- and the separate request is paid only on the one tick that exits
that state, which is also the only tick genuinely ambiguous (the priority-
ordering caveat: a different, higher-priority check can take over the field
without approvals having actually been satisfied). The tests below exercise
that corrected shape directly.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest import mock

import pytest

POLLER = Path(__file__).parent.parent / "presets" / "watch" / "sources" / "gitlab-mr" / "poller.py"
_spec = importlib.util.spec_from_file_location("gitlab_mr_poller_2645", POLLER)
assert _spec is not None and _spec.loader is not None
poller = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(poller)


@pytest.fixture(autouse=True)
def _no_real_glab():
    """Same hermetic default `test_watch_gitlab_mr_poller.py` uses: `_glab_api`
    stubbed to `([], "")` unless a test overrides it -- a non-dict payload,
    so `_fetch_approvals` reads it as *could not tell*, not as unapproved."""
    with mock.patch.object(poller, "_glab_api", return_value=([], "")):
        yield


def _ok(data):
    return (data, "")


def _body(state="opened", target_branch="master", pipeline_status="running",
          detailed_merge_status=""):
    """`detailed_merge_status` defaults to `""` -- nothing blocking the merge
    -- so a test that does not care about approvals does not accidentally
    open (or close) the cost gate by surprise. Pass `"not_approved"` or
    another value explicitly to drive that gate."""
    return {
        "iid": 21803,
        "title": "feat: do the thing",
        "state": state,
        "has_conflicts": False,
        "merge_status": "can_be_merged",
        "detailed_merge_status": detailed_merge_status,
        "head_pipeline": {"id": "9", "status": pipeline_status},
        "web_url": "https://example.com/mr/21803",
        "user_notes_count": 0,
        "target_branch": target_branch,
    }


def _approvals(approved):
    return ({"approved": approved, "approved_by": []}, "")


def _no_call(*_a, **_kw):
    raise AssertionError("_glab_api must not be called on this poll")


# ---------------------------------------------------------------------------
# approved -- rising edge only, and it needs a positive control that an
# unrelated, pre-existing event (`merged`) still fires so a defect in the
# new wiring cannot hide behind two fixtures that both happen to emit
# nothing.
# ---------------------------------------------------------------------------

def test_exiting_not_approved_emits_approved_via_the_confirm_request() -> None:
    """The one tick genuinely worth the separate request: `detailed_merge_
    status` was `not_approved` last poll and is not this poll, so the
    approvals endpoint is asked to confirm what took its place."""
    state = {"mr_state": "opened", "pipeline_status": "running",
             "approved": False, "detailed_status": "not_approved"}
    with mock.patch.object(poller, "_fetch",
                           return_value=_ok(_body(detailed_merge_status="mergeable"))), \
            mock.patch.object(poller, "_glab_api", return_value=_approvals(True)):
        events, new_state = poller.poll(state, {"id": "21803"})
    assert any(e["event"] == "approved" for e in events)
    assert new_state["approved"] is True


def test_not_approved_is_answered_free_with_no_request() -> None:
    """`detailed_merge_status == "not_approved"` is not a hint to check the
    separate endpoint -- it already is the answer, so no request is made."""
    state = {"mr_state": "opened", "pipeline_status": "running", "approved": False}
    with mock.patch.object(poller, "_fetch",
                           return_value=_ok(_body(detailed_merge_status="not_approved"))), \
            mock.patch.object(poller, "_glab_api", side_effect=_no_call):
        events, new_state = poller.poll(state, {"id": "21803"})
    assert not any(e["event"] == "approved" for e in events)
    assert new_state["approved"] is False
    assert new_state["detailed_status"] == "not_approved"


def test_a_standing_not_approved_streak_costs_nothing_on_any_tick() -> None:
    """The regression this design went through self-review to avoid: three
    consecutive polls held at `not_approved` must cost zero separate
    requests, not one per tick."""
    state: dict = {}
    with mock.patch.object(poller, "_glab_api", side_effect=_no_call):
        for _ in range(3):
            with mock.patch.object(poller, "_fetch",
                                   return_value=_ok(_body(detailed_merge_status="not_approved"))):
                events, state = poller.poll(state, {"id": "21803"})
            assert not any(e["event"] == "approved" for e in events)
    assert state["approved"] is False


def test_already_approved_no_change_emits_nothing() -> None:
    """Once confirmed and nothing currently disputes it, no request is made
    to re-confirm -- `not_approved` is not present, so it is neither the
    free answer nor the transition-out edge."""
    state = {"mr_state": "opened", "pipeline_status": "running",
             "approved": True, "detailed_status": "mergeable"}
    with mock.patch.object(poller, "_fetch",
                           return_value=_ok(_body(detailed_merge_status="mergeable"))), \
            mock.patch.object(poller, "_glab_api", side_effect=_no_call):
        events, new_state = poller.poll(state, {"id": "21803"})
    assert not any(e["event"] == "approved" for e in events)
    assert new_state["approved"] is True


def test_first_poll_never_reports_a_confirmed_approved_state() -> None:
    """There is no earlier `detailed_status` to have exited on a first poll,
    so `approved` can only ever be `None` (unconfirmed) or the free `False`
    -- never a confirmed `True` -- until a later poll actually observes the
    transition out of `not_approved`."""
    with mock.patch.object(poller, "_fetch",
                           return_value=_ok(_body(detailed_merge_status="mergeable"))), \
            mock.patch.object(poller, "_glab_api", side_effect=_no_call):
        events, new_state = poller.poll({}, {"id": "21803"})
    assert not any(e["event"] == "approved" for e in events)
    assert new_state["approved"] is None


def test_confirm_request_failure_carries_last_known_value_forward() -> None:
    """A lookup that could not settle on the one tick that mattered must not
    manufacture a false rising edge, and must not overwrite what was known."""
    state = {"mr_state": "opened", "pipeline_status": "running",
             "approved": False, "detailed_status": "not_approved"}
    with mock.patch.object(poller, "_fetch",
                           return_value=_ok(_body(detailed_merge_status="mergeable"))), \
            mock.patch.object(poller, "_glab_api", return_value=(None, "ERROR: glab timed out")):
        events, new_state = poller.poll(state, {"id": "21803"})
    assert not any(e["event"] == "approved" for e in events)
    assert new_state["approved"] is False


def test_approved_flip_and_merged_still_fires() -> None:
    """The issue's own worked example, driven through the real three-tick
    shape: not_approved (free, no fire) -> exits it and is confirmed True
    (fires once) -> merges (pre-existing, unrelated event still fires)."""
    state: dict = {}
    with mock.patch.object(poller, "_glab_api", side_effect=_no_call), \
            mock.patch.object(poller, "_fetch",
                              return_value=_ok(_body(detailed_merge_status="not_approved"))):
        events, state = poller.poll(state, {"id": "21803"})
    assert not any(e["event"] == "approved" for e in events)

    with mock.patch.object(poller, "_fetch",
                           return_value=_ok(_body(detailed_merge_status="mergeable"))), \
            mock.patch.object(poller, "_glab_api", return_value=_approvals(True)):
        events, state = poller.poll(state, {"id": "21803"})
    assert sum(1 for e in events if e["event"] == "approved") == 1

    with mock.patch.object(poller, "_glab_api", side_effect=_no_call), \
            mock.patch.object(poller, "_fetch",
                              return_value=_ok(_body(state="merged", detailed_merge_status="mergeable"))):
        events, state = poller.poll(state, {"id": "21803"})
    assert any(e["event"] == "merged" for e in events)


# ---------------------------------------------------------------------------
# retargeted -- same rising-edge shape, but the "previous" side must be a
# genuinely known branch: the first poll cannot say what branch an MR used
# to target before this source ever looked at it.
# ---------------------------------------------------------------------------

def test_target_branch_change_emits_retargeted() -> None:
    state = {"mr_state": "opened", "pipeline_status": "running", "target_branch": "master"}
    with mock.patch.object(poller, "_fetch", return_value=_ok(_body(target_branch="release/1.0"))):
        events, new_state = poller.poll(state, {"id": "21803"})
    assert any(e["event"] == "retargeted" for e in events)
    assert new_state["target_branch"] == "release/1.0"


def test_same_target_branch_emits_nothing() -> None:
    state = {"mr_state": "opened", "pipeline_status": "running", "target_branch": "master"}
    with mock.patch.object(poller, "_fetch", return_value=_ok(_body(target_branch="master"))):
        events, _ = poller.poll(state, {"id": "21803"})
    assert not any(e["event"] == "retargeted" for e in events)


def test_first_poll_never_fires_retargeted() -> None:
    with mock.patch.object(poller, "_fetch", return_value=_ok(_body(target_branch="release/1.0"))):
        events, new_state = poller.poll({}, {"id": "21803"})
    assert not any(e["event"] == "retargeted" for e in events)
    assert new_state["target_branch"] == "release/1.0"


# ---------------------------------------------------------------------------
# cost: approvals is a separate GitLab request, so watching it must not add
# a request on the common path -- exactly the "once per red streak, not
# once per tick" budget #509 already pinned for the failing-job lookup, and
# the property the first version of this gate broke (see the module
# docstring above).
# ---------------------------------------------------------------------------

def test_nothing_blocking_on_approval_costs_no_extra_request() -> None:
    state = {"mr_state": "opened", "pipeline_status": "running", "detailed_status": ""}
    with mock.patch.object(poller, "_fetch",
                           return_value=_ok(_body(detailed_merge_status=""))), \
            mock.patch.object(poller, "_glab_api", side_effect=_no_call):
        poller.poll(state, {"id": "21803"})
