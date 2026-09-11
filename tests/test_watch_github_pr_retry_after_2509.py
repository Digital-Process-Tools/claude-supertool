"""Rate-limit back-off on `github-pr`'s poller (#2509). Same must-fire /
must-not-fire pair as gh-branch's own test, over `_fetch`'s error arm."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest import mock

POLLER = Path(__file__).parent.parent / "presets" / "watch" / "sources" / "github-pr" / "poller.py"
_spec = importlib.util.spec_from_file_location("github_pr_poller_retry", POLLER)
assert _spec is not None and _spec.loader is not None
poller = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(poller)


def _ctx(number="1448"):
    return {"source": "github-pr", "id": number, "only": []}


def test_rate_limit_failure_attaches_retry_after() -> None:
    with mock.patch.object(poller, "_fetch",
                           return_value=(None, "ERROR: GitHub API rate limit exceeded. Wait a few minutes and retry.")), \
         mock.patch.object(poller.ratelimit, "fetch_reset_iso",
                           return_value=("2026-09-11T18:00:00Z", "")):
        events, new_state = poller.poll({}, _ctx())
    assert len(events) == 1, events
    assert events[0]["payload"]["retry_after"] == "2026-09-11T18:00:00Z"
    assert new_state["retry_after"] == "2026-09-11T18:00:00Z"


def test_a_non_rate_limit_failure_never_carries_retry_after() -> None:
    with mock.patch.object(poller, "_fetch",
                           return_value=(None, "ERROR: gh timed out looking up PR #1448")), \
         mock.patch.object(poller.ratelimit, "fetch_reset_iso",
                           return_value=("2026-09-11T18:00:00Z", "")):
        events, new_state = poller.poll({}, _ctx())
    assert len(events) == 1, events
    assert "retry_after" not in events[0]["payload"]
    assert "retry_after" not in new_state

