"""Unit tests for the gitlab-mr poller source — state diff logic."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

POLLER = Path(__file__).parent.parent / "presets" / "watch" / "sources" / "gitlab-mr" / "poller.py"
_spec = importlib.util.spec_from_file_location("gitlab_mr_poller", POLLER)
assert _spec is not None and _spec.loader is not None
poller = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(poller)


def _mr(state="opened", pipeline_status="running", pipeline_id="9", conflicts=False,
        user_notes_count=0, **kw):
    return {
        "iid": 21803,
        "title": kw.get("title", "feat: do the thing"),
        "state": state,
        "has_conflicts": conflicts,
        "head_pipeline": {"id": pipeline_id, "status": pipeline_status},
        "web_url": "https://example.com/mr/21803",
        "user_notes_count": user_notes_count,
    }


def test_no_change_emits_nothing() -> None:
    state = {"mr_state": "opened", "pipeline_status": "running", "has_conflicts": False}
    with mock.patch.object(poller, "_fetch", return_value=_mr()):
        events, new_state = poller.poll(state, {"id": "21803"})
    assert events == []
    assert new_state["mr_state"] == "opened"


def test_pipeline_running_to_failed_emits_failed() -> None:
    state = {"mr_state": "opened", "pipeline_status": "running"}
    with mock.patch.object(poller, "_fetch", return_value=_mr(pipeline_status="failed")):
        events, _ = poller.poll(state, {"id": "21803"})
    assert len(events) == 1
    assert events[0]["event"] == "pipeline_failed"
    assert events[0]["payload"]["url"] == "https://example.com/mr/21803"
    assert events[0]["notify_title"]


def test_pipeline_running_to_success_emits_succeeded() -> None:
    state = {"mr_state": "opened", "pipeline_status": "running"}
    with mock.patch.object(poller, "_fetch", return_value=_mr(pipeline_status="success")):
        events, _ = poller.poll(state, {"id": "21803"})
    assert any(e["event"] == "pipeline_succeeded" for e in events)


def test_pipeline_pending_to_running_emits_running() -> None:
    state = {"mr_state": "opened", "pipeline_status": "pending"}
    with mock.patch.object(poller, "_fetch", return_value=_mr(pipeline_status="running")):
        events, _ = poller.poll(state, {"id": "21803"})
    assert any(e["event"] == "pipeline_running" for e in events)


def test_merge_emits_merged() -> None:
    state = {"mr_state": "opened", "pipeline_status": "success"}
    with mock.patch.object(poller, "_fetch", return_value=_mr(state="merged", pipeline_status="success")):
        events, _ = poller.poll(state, {"id": "21803"})
    assert any(e["event"] == "merged" for e in events)


def test_conflict_rising_edge_emits_once() -> None:
    state_before = {"has_conflicts": False, "mr_state": "opened", "pipeline_status": "running"}
    with mock.patch.object(poller, "_fetch", return_value=_mr(conflicts=True)):
        events1, new_state = poller.poll(state_before, {"id": "21803"})
    assert any(e["event"] == "conflicts_appeared" for e in events1)
    # Same data again — no new event
    with mock.patch.object(poller, "_fetch", return_value=_mr(conflicts=True)):
        events2, _ = poller.poll(new_state, {"id": "21803"})
    assert all(e["event"] != "conflicts_appeared" for e in events2)


def test_is_terminal_when_merged() -> None:
    assert poller.is_terminal({"mr_state": "merged"}) is True


def test_is_terminal_when_closed() -> None:
    assert poller.is_terminal({"mr_state": "closed"}) is True


def test_is_not_terminal_when_open() -> None:
    assert poller.is_terminal({"mr_state": "opened"}) is False


def test_fetch_failure_returns_no_events() -> None:
    with mock.patch.object(poller, "_fetch", return_value=None):
        events, new_state = poller.poll({"x": 1}, {"id": "21803"})
    assert events == []
    assert new_state == {"x": 1}  # state preserved on transient failure


def test_first_poll_records_notes_count_without_event() -> None:
    """First poll on empty state must NOT fire comment_added — just baseline."""
    with mock.patch.object(poller, "_fetch", return_value=_mr(user_notes_count=3)):
        events, new_state = poller.poll({}, {"id": "21803"})
    assert all(e["event"] != "comment_added" for e in events)
    assert new_state["notes_count"] == 3


def test_notes_count_increase_emits_comment_added() -> None:
    state = {"notes_count": 1, "mr_state": "opened", "pipeline_status": "running"}
    with mock.patch.object(poller, "_fetch", return_value=_mr(user_notes_count=2)):
        events, _ = poller.poll(state, {"id": "21803"})
    matches = [e for e in events if e["event"] == "comment_added"]
    assert len(matches) == 1
    assert matches[0]["payload"]["new_count"] == 1


def test_notes_count_unchanged_no_event() -> None:
    state = {"notes_count": 2, "mr_state": "opened", "pipeline_status": "running"}
    with mock.patch.object(poller, "_fetch", return_value=_mr(user_notes_count=2)):
        events, _ = poller.poll(state, {"id": "21803"})
    assert all(e["event"] != "comment_added" for e in events)


def test_glab_helper_imported_from_mr_op() -> None:
    """The poller must reuse _glab_api from presets/gitlab/mr.py."""
    assert poller._glab_api_cli.__module__ == "gitlab_mr_op"


def test_missing_user_notes_count_does_not_lock_baseline_at_zero() -> None:
    """Absent field keeps notes_count=None so a later real value can still baseline."""
    mr_no_field = {
        "iid": 21803, "title": "x", "state": "opened", "has_conflicts": False,
        "head_pipeline": {"id": "9", "status": "running"},
        "web_url": "https://example.com/mr/21803",
        # user_notes_count intentionally absent
    }
    with mock.patch.object(poller, "_fetch", return_value=mr_no_field):
        events, new_state = poller.poll({}, {"id": "21803"})
    assert all(e["event"] != "comment_added" for e in events)
    assert new_state["notes_count"] is None


def test_notes_count_field_disappearing_skips_event() -> None:
    """If notes_count drops to None on a later poll, no comparison, no event."""
    state = {"notes_count": 5, "mr_state": "opened", "pipeline_status": "running"}
    mr_no_field = {
        "iid": 21803, "title": "x", "state": "opened", "has_conflicts": False,
        "head_pipeline": {"id": "9", "status": "running"},
        "web_url": "https://example.com/mr/21803",
    }
    with mock.patch.object(poller, "_fetch", return_value=mr_no_field):
        events, _ = poller.poll(state, {"id": "21803"})
    assert all(e["event"] != "comment_added" for e in events)
