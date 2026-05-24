"""Unit tests for the github-pr watcher source — state diff + rollup logic."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

POLLER = Path(__file__).parent.parent / "presets" / "watch" / "sources" / "github-pr" / "poller.py"
_spec = importlib.util.spec_from_file_location("github_pr_poller", POLLER)
assert _spec is not None and _spec.loader is not None
poller = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(poller)


def _pr(
    state="OPEN", mergeable="MERGEABLE", review="", checks=None, title="some PR",
    comments=None,
):
    return {
        "state": state,
        "mergeable": mergeable,
        "title": title,
        "url": "https://github.com/org/repo/pull/42",
        "number": 42,
        "headRefName": "feature/x",
        "isDraft": False,
        "reviewDecision": review,
        "statusCheckRollup": checks or [],
        "comments": comments or [],
    }


# ---- rollup -----------------------------------------------------------------

def test_rollup_empty_returns_empty() -> None:
    assert poller._rollup_state(None) == ""
    assert poller._rollup_state([]) == ""


def test_rollup_all_success() -> None:
    rollup = [
        {"status": "COMPLETED", "conclusion": "SUCCESS"},
        {"status": "COMPLETED", "conclusion": "SUCCESS"},
    ]
    assert poller._rollup_state(rollup) == "SUCCESS"


def test_rollup_one_failure_dominates() -> None:
    rollup = [
        {"status": "COMPLETED", "conclusion": "SUCCESS"},
        {"status": "COMPLETED", "conclusion": "FAILURE"},
    ]
    assert poller._rollup_state(rollup) == "FAILURE"


def test_rollup_in_progress_pending() -> None:
    rollup = [
        {"status": "COMPLETED", "conclusion": "SUCCESS"},
        {"status": "IN_PROGRESS", "conclusion": ""},
    ]
    assert poller._rollup_state(rollup) == "PENDING"


def test_rollup_handles_state_flavour() -> None:
    """Statuses (vs check runs) use `state`, not status/conclusion."""
    rollup = [{"state": "FAILURE"}]
    assert poller._rollup_state(rollup) == "FAILURE"


def test_rollup_neutral_skipped_treated_as_success() -> None:
    rollup = [
        {"status": "COMPLETED", "conclusion": "SUCCESS"},
        {"status": "COMPLETED", "conclusion": "NEUTRAL"},
        {"status": "COMPLETED", "conclusion": "SKIPPED"},
    ]
    assert poller._rollup_state(rollup) == "SUCCESS"


# ---- poll -------------------------------------------------------------------

def test_no_change_emits_nothing() -> None:
    state = {"pr_state": "OPEN", "checks_state": "SUCCESS", "review_decision": "", "mergeable": "MERGEABLE"}
    with mock.patch.object(poller, "_fetch", return_value=_pr(checks=[{"status": "COMPLETED", "conclusion": "SUCCESS"}])):
        events, _ = poller.poll(state, {"id": "42"})
    assert events == []


def test_checks_failed_emits_failed() -> None:
    state = {"checks_state": "PENDING"}
    rollup = [{"status": "COMPLETED", "conclusion": "FAILURE"}]
    with mock.patch.object(poller, "_fetch", return_value=_pr(checks=rollup)):
        events, _ = poller.poll(state, {"id": "42"})
    assert any(e["event"] == "checks_failed" for e in events)


def test_checks_succeeded_emits_succeeded() -> None:
    state = {"checks_state": "PENDING"}
    rollup = [{"status": "COMPLETED", "conclusion": "SUCCESS"}]
    with mock.patch.object(poller, "_fetch", return_value=_pr(checks=rollup)):
        events, _ = poller.poll(state, {"id": "42"})
    assert any(e["event"] == "checks_succeeded" for e in events)


def test_merged_emits_merged_and_is_terminal() -> None:
    state = {"pr_state": "OPEN"}
    with mock.patch.object(poller, "_fetch", return_value=_pr(state="MERGED")):
        events, new_state = poller.poll(state, {"id": "42"})
    assert any(e["event"] == "merged" for e in events)
    assert poller.is_terminal(new_state) is True


def test_closed_emits_closed_and_is_terminal() -> None:
    state = {"pr_state": "OPEN"}
    with mock.patch.object(poller, "_fetch", return_value=_pr(state="CLOSED")):
        events, new_state = poller.poll(state, {"id": "42"})
    assert any(e["event"] == "closed" for e in events)
    assert poller.is_terminal(new_state) is True


def test_review_approved_emits_event() -> None:
    state = {"review_decision": "REVIEW_REQUIRED"}
    with mock.patch.object(poller, "_fetch", return_value=_pr(review="APPROVED")):
        events, _ = poller.poll(state, {"id": "42"})
    assert any(e["event"] == "review_approved" for e in events)


def test_changes_requested_emits_event() -> None:
    state = {"review_decision": ""}
    with mock.patch.object(poller, "_fetch", return_value=_pr(review="CHANGES_REQUESTED")):
        events, _ = poller.poll(state, {"id": "42"})
    assert any(e["event"] == "review_changes_requested" for e in events)


def test_conflict_rising_edge_emits_once() -> None:
    state = {"mergeable": "MERGEABLE"}
    with mock.patch.object(poller, "_fetch", return_value=_pr(mergeable="CONFLICTING")):
        events1, new_state = poller.poll(state, {"id": "42"})
    assert any(e["event"] == "conflicts_appeared" for e in events1)
    # Same state again — must not re-fire
    with mock.patch.object(poller, "_fetch", return_value=_pr(mergeable="CONFLICTING")):
        events2, _ = poller.poll(new_state, {"id": "42"})
    assert all(e["event"] != "conflicts_appeared" for e in events2)


def test_fetch_failure_preserves_state() -> None:
    state = {"checks_state": "PENDING", "marker": "keep"}
    with mock.patch.object(poller, "_fetch", return_value=None):
        events, new_state = poller.poll(state, {"id": "42"})
    assert events == []
    assert new_state == state


def test_is_terminal_open_returns_false() -> None:
    assert poller.is_terminal({"pr_state": "OPEN"}) is False


def test_first_poll_records_comment_count_without_event() -> None:
    """First poll on empty state must NOT fire comment_added — just baseline."""
    comments = [{"author": {"login": "alice"}, "body": "hi"}]
    with mock.patch.object(poller, "_fetch", return_value=_pr(comments=comments)):
        events, new_state = poller.poll({}, {"id": "42"})
    assert all(e["event"] != "comment_added" for e in events)
    assert new_state["comments_count"] == 1


def test_comment_count_increase_emits_comment_added() -> None:
    state = {"comments_count": 1}
    comments = [
        {"author": {"login": "alice"}, "body": "hi"},
        {"author": {"login": "bob"}, "body": "second"},
    ]
    with mock.patch.object(poller, "_fetch", return_value=_pr(comments=comments)):
        events, _ = poller.poll(state, {"id": "42"})
    matches = [e for e in events if e["event"] == "comment_added"]
    assert len(matches) == 1
    assert matches[0]["payload"]["author"] == "bob"
    assert matches[0]["payload"]["new_count"] == 1


def test_comment_count_unchanged_no_event() -> None:
    state = {"comments_count": 2}
    comments = [
        {"author": {"login": "alice"}, "body": "hi"},
        {"author": {"login": "bob"}, "body": "second"},
    ]
    with mock.patch.object(poller, "_fetch", return_value=_pr(comments=comments)):
        events, _ = poller.poll(state, {"id": "42"})
    assert all(e["event"] != "comment_added" for e in events)


def test_gh_helper_imported_from_pr_op() -> None:
    """The poller must use the existing gh wrapper from presets/github/pr.py."""
    import inspect
    assert poller._gh.__module__ == "github_pr_op"
    # _gh signature: (args, timeout=10)
    sig = inspect.signature(poller._gh)
    assert "args" in sig.parameters
