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



def _ok(data):
    """The `(payload, "")` pair `_fetch` returns on a successful lookup (#541)."""
    return (data, "")


def _unreachable(msg="ERROR: could not look"):
    """The `(None, why)` pair `_fetch` returns when it could not look (#541)."""
    return (None, msg)



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



def _leg(name: str, conclusion: str, started: str, completed: str) -> dict:
    """One `statusCheckRollup` CheckRun node, in the shape gh returns."""
    return {
        "name": name,
        "status": "COMPLETED" if conclusion else "IN_PROGRESS",
        "conclusion": conclusion,
        "startedAt": started,
        "completedAt": completed,
    }


def test_rollup_supersession_2070() -> None:
    """#2070 — a check run a later same-named run replaced must not vote.

    #1803/#1804 gave `git-branch`/`gh-pr` renders this arithmetic through
    `_checks.github_superseded()`; the watch poller had its own third copy
    that never got the sweep. Reported case (jbkkz, PR #198): the successful
    `fragment` leg started three seconds after the cancelled one completed —
    exactly #1792's discriminator, not #1640's overlap.
    """
    # must-not-fire: the cancelled leg is superseded by the later success.
    superseded_then_fixed = [
        _leg("fragment", "CANCELLED",
             "2026-08-27T10:00:00Z", "2026-08-27T10:00:30Z"),
        _leg("fragment", "SUCCESS",
             "2026-08-27T10:00:33Z", "2026-08-27T10:01:10Z"),
    ]
    assert poller._rollup_state(superseded_then_fixed) == "SUCCESS"

    # must-fire: a genuine failure with no later same-named run still counts.
    genuine_failure = [
        _leg("fragment", "FAILURE",
             "2026-08-27T10:00:00Z", "2026-08-27T10:00:30Z"),
    ]
    assert poller._rollup_state(genuine_failure) == "FAILURE"

    # must-fire: #1640's overlapping same-named code-scanning runs both
    # count — wall clocks overlap, so neither supersedes the other, and a
    # failure in either still reddens the rollup.
    overlapping_same_name = [
        _leg("Analyze (python)", "SUCCESS",
             "2026-08-13T22:23:05Z", "2026-08-13T22:25:52Z"),
        _leg("Analyze (python)", "FAILURE",
             "2026-08-13T22:23:05Z", "2026-08-13T22:26:13Z"),
    ]
    assert poller._rollup_state(overlapping_same_name) == "FAILURE"


# ---- poll -------------------------------------------------------------------

def test_no_change_emits_nothing() -> None:
    state = {"pr_state": "OPEN", "checks_state": "SUCCESS", "review_decision": "", "mergeable": "MERGEABLE"}
    with mock.patch.object(poller, "_fetch", return_value=_ok(_pr(checks=[{"status": "COMPLETED", "conclusion": "SUCCESS"}]))):
        events, _ = poller.poll(state, {"id": "42"})
    assert events == []


def test_checks_failed_emits_failed() -> None:
    state = {"checks_state": "PENDING"}
    rollup = [{"status": "COMPLETED", "conclusion": "FAILURE"}]
    with mock.patch.object(poller, "_fetch", return_value=_ok(_pr(checks=rollup))):
        events, _ = poller.poll(state, {"id": "42"})
    assert any(e["event"] == "checks_failed" for e in events)


def test_checks_succeeded_emits_succeeded() -> None:
    state = {"checks_state": "PENDING"}
    rollup = [{"status": "COMPLETED", "conclusion": "SUCCESS"}]
    with mock.patch.object(poller, "_fetch", return_value=_ok(_pr(checks=rollup))):
        events, _ = poller.poll(state, {"id": "42"})
    assert any(e["event"] == "checks_succeeded" for e in events)


def test_merged_emits_merged_and_is_terminal() -> None:
    state = {"pr_state": "OPEN"}
    with mock.patch.object(poller, "_fetch", return_value=_ok(_pr(state="MERGED"))):
        events, new_state = poller.poll(state, {"id": "42"})
    assert any(e["event"] == "merged" for e in events)
    assert poller.is_terminal(new_state) is True


def test_closed_emits_closed_and_is_terminal() -> None:
    state = {"pr_state": "OPEN"}
    with mock.patch.object(poller, "_fetch", return_value=_ok(_pr(state="CLOSED"))):
        events, new_state = poller.poll(state, {"id": "42"})
    assert any(e["event"] == "closed" for e in events)
    assert poller.is_terminal(new_state) is True


def test_review_approved_emits_event() -> None:
    state = {"review_decision": "REVIEW_REQUIRED"}
    with mock.patch.object(poller, "_fetch", return_value=_ok(_pr(review="APPROVED"))):
        events, _ = poller.poll(state, {"id": "42"})
    assert any(e["event"] == "review_approved" for e in events)


def test_changes_requested_emits_event() -> None:
    state = {"review_decision": ""}
    with mock.patch.object(poller, "_fetch", return_value=_ok(_pr(review="CHANGES_REQUESTED"))):
        events, _ = poller.poll(state, {"id": "42"})
    assert any(e["event"] == "review_changes_requested" for e in events)


def test_conflict_rising_edge_emits_once() -> None:
    state = {"mergeable": "MERGEABLE"}
    with mock.patch.object(poller, "_fetch", return_value=_ok(_pr(mergeable="CONFLICTING"))):
        events1, new_state = poller.poll(state, {"id": "42"})
    assert any(e["event"] == "conflicts_appeared" for e in events1)
    # Same state again — must not re-fire
    with mock.patch.object(poller, "_fetch", return_value=_ok(_pr(mergeable="CONFLICTING"))):
        events2, _ = poller.poll(new_state, {"id": "42"})
    assert all(e["event"] != "conflicts_appeared" for e in events2)


def test_fetch_failure_reports_and_preserves_state() -> None:
    """`events == []` was this test's assertion until #541 — the defect written
    down as a requirement. What survives is the state guarantee: every field the
    next poll compares against is carried through the outage untouched."""
    state = {"checks_state": "PENDING", "marker": "keep"}
    with mock.patch.object(poller, "_fetch", return_value=_unreachable()):
        events, new_state = poller.poll(state, {"id": "42"})
    assert [e["event"] for e in events] == ["pr_unreachable"]
    assert new_state["checks_state"] == "PENDING"
    assert new_state["marker"] == "keep"
    assert new_state["lookup"] == "unavailable"


def test_is_terminal_open_returns_false() -> None:
    assert poller.is_terminal({"pr_state": "OPEN"}) is False


def test_first_poll_records_comment_count_without_event() -> None:
    """First poll on empty state must NOT fire comment_added — just baseline."""
    comments = [{"author": {"login": "alice"}, "body": "hi"}]
    with mock.patch.object(poller, "_fetch", return_value=_ok(_pr(comments=comments))):
        events, new_state = poller.poll({}, {"id": "42"})
    assert all(e["event"] != "comment_added" for e in events)
    assert new_state["comments_count"] == 1


def test_comment_count_increase_emits_comment_added() -> None:
    state = {"comments_count": 1}
    comments = [
        {"author": {"login": "alice"}, "body": "hi"},
        {"author": {"login": "bob"}, "body": "second"},
    ]
    with mock.patch.object(poller, "_fetch", return_value=_ok(_pr(comments=comments))):
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
    with mock.patch.object(poller, "_fetch", return_value=_ok(_pr(comments=comments))):
        events, _ = poller.poll(state, {"id": "42"})
    assert all(e["event"] != "comment_added" for e in events)


def test_gh_helper_imported_from_pr_op() -> None:
    """The poller must use the existing gh wrapper from presets/github/pr.py."""
    import inspect
    assert poller._gh.__module__ == "github_pr_op"
    # _gh signature: (args, timeout=10)
    sig = inspect.signature(poller._gh)
    assert "args" in sig.parameters
