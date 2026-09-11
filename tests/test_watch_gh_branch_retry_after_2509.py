"""Rate-limit back-off on `gh-branch`'s poller (#2509).

A rate-limit-shaped `error` from `_snapshot` must carry `retry_after` on the
emitted `branch_unreachable` event and in the state the dispatcher reads to
decide how long to sleep; any other failure must not (the must-fire /
must-not-fire pair the issue itself asks for).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest import mock

POLLER = Path(__file__).parent.parent / "presets" / "watch" / "sources" / "gh-branch" / "poller.py"
_spec = importlib.util.spec_from_file_location("gh_branch_poller_retry", POLLER)
assert _spec is not None and _spec.loader is not None
poller = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(poller)


def _snap(error):
    return ("", "", "", "", error, False)


def _ctx(ref="main"):
    return {"source": "gh-branch", "id": ref, "only": []}


def test_rate_limit_failure_attaches_retry_after() -> None:
    """Must-fire half."""
    with mock.patch.object(poller, "_snapshot",
                           return_value=_snap("ERROR: GitHub API rate limit exceeded. Wait a few minutes.")), \
         mock.patch.object(poller.ratelimit, "fetch_reset_iso",
                           return_value=("2026-09-11T18:00:00Z", "")):
        events, new_state = poller.poll({}, _ctx())
    assert len(events) == 1, events
    assert events[0]["payload"]["retry_after"] == "2026-09-11T18:00:00Z"
    assert new_state["retry_after"] == "2026-09-11T18:00:00Z"


def test_a_non_rate_limit_failure_never_carries_retry_after() -> None:
    """Must-not-fire twin: an ordinary outage must not be mistaken for a
    rate limit, so the field must be entirely absent, not empty."""
    with mock.patch.object(poller, "_snapshot",
                           return_value=_snap("ERROR: gh timed out")), \
         mock.patch.object(poller.ratelimit, "fetch_reset_iso",
                           return_value=("2026-09-11T18:00:00Z", "")):
        events, new_state = poller.poll({}, _ctx())
    assert len(events) == 1, events
    assert "retry_after" not in events[0]["payload"]
    assert "retry_after" not in new_state


def test_rate_limit_failure_whose_reset_could_not_be_read_carries_nothing() -> None:
    """A rate-limit-shaped error whose own `gh api rate_limit` lookup fails
    must not manufacture a retry_after -- the third state, never a guess."""
    with mock.patch.object(poller, "_snapshot",
                           return_value=_snap("ERROR: GitHub API rate limit exceeded. Wait a few minutes.")), \
         mock.patch.object(poller.ratelimit, "fetch_reset_iso",
                           return_value=(None, "gh not found")):
        events, new_state = poller.poll({}, _ctx())
    assert "retry_after" not in events[0]["payload"]
    assert "retry_after" not in new_state

