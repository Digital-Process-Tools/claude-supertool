"""`approved` and `retargeted` events for the `gitlab-mr` watch source (#2645).

Before this, the source emitted pipeline and comment transitions but stayed
silent on an MR reaching its approval rule or being retargeted onto a
different branch -- both change what a maintainer does next, and both were
only discoverable by re-reading the MR by hand.

Mirrors `tests/test_watch_gitlab_mr_poller.py`'s own fixture shape (a fresh
`importlib` load of `poller.py`, `_glab_api` stubbed by an autouse fixture)
rather than importing its private `_mr` helper, which has no `target_branch`
or approvals knob and is not this file's to extend.
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
          detailed_merge_status="not_approved"):
    """`detailed_merge_status` defaults to `"not_approved"` -- the free gate
    `poll()` reads before it pays for the separate approvals request -- so
    a test that wants to exercise `_fetch_approvals` does not also have to
    remember to open the gate. Pass `""` for the tests that do not care
    about approvals at all, matching a fixture GitLab never actually
    reports the field on when nothing is blocking the merge."""
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


# ---------------------------------------------------------------------------
# approved -- rising edge only, and it needs a positive control that an
# unrelated, pre-existing event (`merged`) still fires so a defect in the
# new wiring cannot hide behind two fixtures that both happen to emit
# nothing.
# ---------------------------------------------------------------------------

def test_approved_rising_edge_emits_approved() -> None:
    state = {"mr_state": "opened", "pipeline_status": "running", "approved": False}
    with mock.patch.object(poller, "_fetch", return_value=_ok(_body())), \
            mock.patch.object(poller, "_glab_api", return_value=_approvals(True)):
        events, new_state = poller.poll(state, {"id": "21803"})
    assert any(e["event"] == "approved" for e in events)
    assert new_state["approved"] is True


def test_already_approved_no_change_emits_nothing() -> None:
    state = {"mr_state": "opened", "pipeline_status": "running", "approved": True}
    with mock.patch.object(poller, "_fetch", return_value=_ok(_body())), \
            mock.patch.object(poller, "_glab_api", return_value=_approvals(True)):
        events, _ = poller.poll(state, {"id": "21803"})
    assert not any(e["event"] == "approved" for e in events)


def test_first_poll_never_fires_approved_even_if_already_true() -> None:
    """No prior read to compare against -- a first poll on an MR that is
    already approved must not be reported as the moment it became so."""
    with mock.patch.object(poller, "_fetch", return_value=_ok(_body())), \
            mock.patch.object(poller, "_glab_api", return_value=_approvals(True)):
        events, new_state = poller.poll({}, {"id": "21803"})
    assert not any(e["event"] == "approved" for e in events)
    assert new_state["approved"] is True


def test_approvals_lookup_failure_carries_last_known_value_forward() -> None:
    """A lookup that could not settle must not manufacture a false rising
    edge on recovery, and must not overwrite what was already known."""
    state = {"mr_state": "opened", "pipeline_status": "running", "approved": False}
    with mock.patch.object(poller, "_fetch", return_value=_ok(_body())), \
            mock.patch.object(poller, "_glab_api", return_value=(None, "ERROR: glab timed out")):
        events, new_state = poller.poll(state, {"id": "21803"})
    assert not any(e["event"] == "approved" for e in events)
    assert new_state["approved"] is False


def test_approved_flip_and_merged_still_fires() -> None:
    """The issue's own worked example: a poller fixture that flips `approved`
    between two polls fires it once, and `merged` -- pre-existing, unrelated
    -- still fires on the same fixture."""
    state: dict = {}
    with mock.patch.object(poller, "_fetch", return_value=_ok(_body())), \
            mock.patch.object(poller, "_glab_api", return_value=_approvals(False)):
        events, state = poller.poll(state, {"id": "21803"})
    assert not any(e["event"] == "approved" for e in events)

    with mock.patch.object(poller, "_fetch", return_value=_ok(_body())), \
            mock.patch.object(poller, "_glab_api", return_value=_approvals(True)):
        events, state = poller.poll(state, {"id": "21803"})
    assert sum(1 for e in events if e["event"] == "approved") == 1

    with mock.patch.object(poller, "_fetch", return_value=_ok(_body(state="merged"))), \
            mock.patch.object(poller, "_glab_api", return_value=_approvals(True)):
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
# once per tick" budget #509 already pinned for the failing-job lookup.
# ---------------------------------------------------------------------------

def test_nothing_blocking_on_approval_costs_no_extra_request() -> None:
    calls: list[str] = []

    def _api(endpoint, *_a):
        calls.append(endpoint)
        return ([], "")

    state = {"mr_state": "opened", "pipeline_status": "running", "detailed_status": ""}
    with mock.patch.object(poller, "_fetch",
                           return_value=_ok(_body(detailed_merge_status=""))), \
            mock.patch.object(poller, "_glab_api", side_effect=_api):
        poller.poll(state, {"id": "21803"})
    assert calls == []
