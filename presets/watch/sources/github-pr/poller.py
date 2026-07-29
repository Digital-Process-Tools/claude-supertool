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
import subprocess
from pathlib import Path
from typing import Any

INTERVAL = 30

# Two imports from presets/github, two jobs — the split `gh-run` already made:
#   `_gh`           — the shared gh invoker (timeout, encoding, capture).
#   `_format_error` — the classifier that turns gh stderr into "gh CLI not
#                     authenticated. Run: gh auth login" or "not found". It
#                     lives in run.py rather than pr.py, and it is generic over
#                     (resource, identifier), so the PR watcher borrows it
#                     rather than growing a third copy of the same if-ladder.
_GITHUB_DIR = Path(__file__).parents[3] / "github"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _GITHUB_DIR / filename)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_gh = _load("github_pr_op", "pr.py")._gh  # type: ignore[attr-defined]
_format_error = _load("github_run_op", "run.py")._format_error  # type: ignore[attr-defined]

LOOKUP_OK = "ok"
LOOKUP_UNAVAILABLE = "unavailable"

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


def _fetch(number: str) -> tuple[dict[str, Any] | None, str]:
    """`(pr, "")` when GitHub answered, `(None, why)` when we could not look.

    Every failure path carries a reason. Collapsing them to `None` was the #541
    defect — a renamed repo, an expired token and a healthy PR with nothing new
    all produced the same answer, forever.

    The exception arms are new rather than moved: `_gh` was called bare here, so
    `gh` missing from PATH raised `FileNotFoundError` straight out of `poll()`.
    The dispatcher caught it, wrote `last_error` into the state file and slept —
    which is not silence in the strictest sense, but it is silence on every
    surface a person actually looks at.
    """
    try:
        r = _gh(["pr", "view", number, "--json", _VIEW_FIELDS])
    except FileNotFoundError:
        return None, "ERROR: gh not found — install from https://cli.github.com"
    except subprocess.TimeoutExpired:
        return None, f"ERROR: gh timed out looking up PR #{number}"
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"ERROR: gh could not run for PR #{number}: {e}"
    if r.returncode != 0:
        return None, _format_error(r.stderr or "", "Pull request", number)
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None, f"ERROR: invalid JSON from gh for PR #{number}"
    if not isinstance(data, dict):
        return None, f"ERROR: unexpected payload shape from gh for PR #{number}"
    return data, ""


def poll(state: dict, ctx: dict) -> tuple[list[dict], dict]:
    number = ctx["id"]
    data, error = _fetch(number)
    if data is None:
        # Three answers, not two: ok, a finding, and *cannot tell*. Said once
        # per outage, edge-triggered on the lookup flag — an alert that repeats
        # every 30s is one people mute, and a muted alert is the original
        # silence by a longer route.
        #
        # `{**state, ...}` is the recovery guarantee. This source carries five
        # comparison fields (pr_state, checks_state, review_decision, mergeable,
        # comments_count) and every one of them has to survive the outage
        # untouched, or the transition that happened while we were blind is
        # either lost or re-announced. `comments_count` is the sharp one: it is
        # `None` only on a genuine first poll, and resetting it here would make
        # the next successful poll re-baseline and drop every comment left
        # during the outage.
        new_state = {**state, "lookup": LOOKUP_UNAVAILABLE, "error": error}
        if state.get("lookup") == LOOKUP_UNAVAILABLE:
            return [], new_state
        return [{
            "event": "pr_unreachable",
            "payload": {
                "number": str(number),
                "error": error,
                # `last_known_`, not the bare field name: what we could see the
                # last time we could see, not what the PR is doing now. The two
                # having become the same word is the whole bug.
                "last_known_state": str(state.get("pr_state") or ""),
                "last_known_checks": str(state.get("checks_state") or ""),
                "title": str(state.get("title") or ""),
                "url": str(state.get("url") or ""),
            },
            "notify_title": f"#{number} — cannot tell",
            "notify_message": error,
        }], new_state

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
        "lookup": LOOKUP_OK,
    }
    return events, new_state


def is_terminal(state: dict) -> bool:
    return state.get("pr_state") in TERMINAL_PR_STATES
