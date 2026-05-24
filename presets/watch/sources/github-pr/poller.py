"""github-pr watcher source.

Polls a single GitHub pull request via `gh pr view --json` and emits events
when state changes. Terminal when the PR is merged or closed.

Reuses `_gh` from presets/github/pr.py — no duplicated CLI wrapping.

Source plugin contract:
- INTERVAL: int seconds between polls (30s default)
- poll(state, ctx) -> (events, new_state)
- is_terminal(state) -> bool
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

INTERVAL = 30

# Import the existing _gh CLI wrapper from the gh-pr op so we share one source
# of truth for gh invocation, error handling, and timeouts.
_PR_MODULE_PATH = Path(__file__).parents[3] / "github" / "pr.py"
_spec = importlib.util.spec_from_file_location("github_pr_op", _PR_MODULE_PATH)
assert _spec is not None and _spec.loader is not None
_pr_op = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pr_op)
_gh = _pr_op._gh  # type: ignore[attr-defined]

# Fields we ask gh for on each poll. Cheap (single API call) and covers every
# event in events.json. `comments` returns the issue-comments array; we just
# need its length to detect new comments.
_VIEW_FIELDS = (
    "state,mergeable,isDraft,title,url,reviewDecision,"
    "statusCheckRollup,number,headRefName,comments"
)

TERMINAL_PR_STATES = {"MERGED", "CLOSED"}


def _rollup_state(rollup: list | None) -> str:
    """Aggregate gh's statusCheckRollup into a single state string.

    Mirrors the heuristic the GitHub UI uses: FAILURE if any failed, PENDING
    if any still running, SUCCESS if everything passed. Empty list / None
    means no checks configured — we return "" so we don't fire events.
    """
    if not isinstance(rollup, list) or not rollup:
        return ""
    statuses = []
    for c in rollup:
        if not isinstance(c, dict):
            continue
        # Two flavours: check runs (status/conclusion) and statuses (state).
        conclusion = (c.get("conclusion") or "").upper()
        status = (c.get("status") or "").upper()
        state = (c.get("state") or "").upper()
        if status and status not in ("COMPLETED", ""):
            statuses.append("PENDING")
            continue
        if conclusion:
            statuses.append(conclusion)
            continue
        if state:
            statuses.append(state)
    if any(s in ("FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR") for s in statuses):
        return "FAILURE"
    if any(s in ("PENDING", "QUEUED", "IN_PROGRESS", "WAITING") for s in statuses):
        return "PENDING"
    if statuses and all(s in ("SUCCESS", "NEUTRAL", "SKIPPED") for s in statuses):
        return "SUCCESS"
    return ""


def _fetch(number: str) -> dict[str, Any] | None:
    r = _gh(["pr", "view", number, "--json", _VIEW_FIELDS])
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def poll(state: dict, ctx: dict) -> tuple[list[dict], dict]:
    number = ctx["id"]
    data = _fetch(number)
    if data is None:
        return [], state  # transient — try again next tick

    pr_state = str(data.get("state") or "").upper()
    mergeable = str(data.get("mergeable") or "").upper()
    title = str(data.get("title") or f"PR #{number}")
    url = str(data.get("url") or "")
    review_decision = str(data.get("reviewDecision") or "").upper()
    checks_state = _rollup_state(data.get("statusCheckRollup"))
    comments_list = data.get("comments") if isinstance(data.get("comments"), list) else []
    comments_count = len(comments_list)

    prev_pr = state.get("pr_state", "")
    prev_checks = state.get("checks_state", "")
    prev_review = state.get("review_decision", "")
    prev_mergeable = state.get("mergeable", "")
    prev_comments_count = state.get("comments_count")  # None on first poll

    events: list[dict] = []

    # Check-suite transitions
    if checks_state and checks_state != prev_checks:
        if checks_state == "FAILURE":
            events.append({
                "event": "checks_failed",
                "payload": {"url": url, "title": title},
                "notify_title": f"#{number} checks failed",
                "notify_message": title,
            })
        elif checks_state == "SUCCESS":
            events.append({
                "event": "checks_succeeded",
                "payload": {"url": url, "title": title},
                "notify_title": f"#{number} checks ok",
                "notify_message": title,
            })
        elif checks_state == "PENDING" and prev_checks not in ("PENDING", ""):
            events.append({
                "event": "checks_pending",
                "payload": {"url": url, "title": title},
            })

    # Review decision transitions (only fire on real decisions, ignore "")
    if review_decision and review_decision != prev_review:
        if review_decision == "APPROVED":
            events.append({
                "event": "review_approved",
                "payload": {"url": url, "title": title},
                "notify_title": f"#{number} approved",
                "notify_message": title,
            })
        elif review_decision == "CHANGES_REQUESTED":
            events.append({
                "event": "review_changes_requested",
                "payload": {"url": url, "title": title},
                "notify_title": f"#{number} changes requested",
                "notify_message": title,
            })

    # PR terminal transitions
    if pr_state and pr_state != prev_pr:
        if pr_state == "MERGED":
            events.append({
                "event": "merged",
                "payload": {"url": url, "title": title},
                "notify_title": f"#{number} merged",
                "notify_message": title,
            })
        elif pr_state == "CLOSED":
            events.append({
                "event": "closed",
                "payload": {"url": url, "title": title},
                "notify_title": f"#{number} closed",
                "notify_message": title,
            })

    # Comment count rising — new issue-comment(s) added since last poll.
    # First poll (prev_comments_count is None) only records the baseline so
    # we don't fire an event on watch-start.
    if prev_comments_count is not None and comments_count > prev_comments_count:
        delta = comments_count - prev_comments_count
        latest = comments_list[-1] if comments_list else {}
        author = ((latest.get("author") or {}).get("login") if isinstance(latest, dict) else "") or "?"
        events.append({
            "event": "comment_added",
            "payload": {
                "url": url,
                "title": title,
                "author": author,
                "new_count": delta,
            },
            "notify_title": f"#{number} new comment{'s' if delta > 1 else ''}",
            "notify_message": f"by {author}: {title}",
        })

    # Mergeable rising edge — fires when going from MERGEABLE/UNKNOWN to CONFLICTING
    if mergeable == "CONFLICTING" and prev_mergeable != "CONFLICTING":
        events.append({
            "event": "conflicts_appeared",
            "payload": {"url": url, "title": title},
            "notify_title": f"#{number} conflicts",
            "notify_message": title,
        })

    new_state = {
        "pr_state": pr_state,
        "mergeable": mergeable,
        "checks_state": checks_state,
        "review_decision": review_decision,
        "comments_count": comments_count,
        "title": title,
        "url": url,
    }
    return events, new_state


def is_terminal(state: dict) -> bool:
    return state.get("pr_state") in TERMINAL_PR_STATES
