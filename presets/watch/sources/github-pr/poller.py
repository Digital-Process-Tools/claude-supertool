











from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

INTERVAL = 30








_GITHUB_DIR = Path(__file__).parents[3] / "github"
_PRESETS_DIR = _GITHUB_DIR.parent










if str(_PRESETS_DIR) not in sys.path:
    sys.path.insert(0, str(_PRESETS_DIR))
import _checks  


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _GITHUB_DIR / filename)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_gh = _load("github_pr_op", "pr.py")._gh  
_format_error = _load("github_run_op", "run.py")._format_error  

_WATCH_DIR = Path(__file__).parents[2]


def _load_watch(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _WATCH_DIR / filename)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod





ratelimit = _load_watch("watch_github_pr_ratelimit", "ratelimit.py")

LOOKUP_OK = "ok"
LOOKUP_UNAVAILABLE = "unavailable"




_VIEW_FIELDS = (
    "state,mergeable,isDraft,title,url,reviewDecision,"
    "statusCheckRollup,number,headRefName,comments"
)

TERMINAL_PR_STATES = {"MERGED", "CLOSED"}






















AUTHORSHIP_VIEWER = "true"
AUTHORSHIP_OTHER = "false"
AUTHORSHIP_MIXED = "mixed"
AUTHORSHIP_UNKNOWN = "unknown"


def _author_is_viewer(new_comments: list) -> str:


















    if not new_comments:
        return AUTHORSHIP_UNKNOWN
    flags = []
    for row in new_comments:
        if not isinstance(row, dict):
            return AUTHORSHIP_UNKNOWN
        flag = row.get("viewerDidAuthor")
        if not isinstance(flag, bool):
            return AUTHORSHIP_UNKNOWN
        flags.append(flag)
    if all(flags):
        return AUTHORSHIP_VIEWER
    if not any(flags):
        return AUTHORSHIP_OTHER
    return AUTHORSHIP_MIXED


def _rollup_state(rollup: list | None) -> str:
















    if not isinstance(rollup, list) or not rollup:
        return ""
    checks = [c for c in rollup if isinstance(c, dict)]
    if not checks:
        return ""
    live = _checks.github_live_states(checks)
    if not live:
        return ""
    if any(_checks.is_red(s) for s in live):
        return "FAILURE"
    if any(_checks.bucket(s) == "pending" for s in live):
        return "PENDING"
    if all(_checks.bucket(s) == "passed" or s in _checks.BENIGN_STATES
           for s in live):
        return "SUCCESS"
    return ""


def _fetch(number: str) -> tuple[dict[str, Any] | None, str]:












    try:
        r = _gh(["pr", "view", number, "--json", _VIEW_FIELDS])
    except FileNotFoundError:
        return None, "ERROR: gh not found — install the GitHub CLI"
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

















        extra = ratelimit.unreachable_extra(error)
        new_state = {**state, "lookup": LOOKUP_UNAVAILABLE, "error": error, **extra}
        if state.get("lookup") == LOOKUP_UNAVAILABLE:
            return [], new_state
        payload = {
            "number": str(number),
            "error": error,



            "last_known_state": str(state.get("pr_state") or ""),
            "last_known_checks": str(state.get("checks_state") or ""),
            "title": str(state.get("title") or ""),
            "url": str(state.get("url") or ""),
        }
        if "retry_after" in extra:
            payload["retry_after"] = extra["retry_after"]
        return [{
            "event": "pr_unreachable",
            "payload": payload,
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
    prev_comments_count = state.get("comments_count")  

    events: list[dict] = []


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




    if prev_comments_count is not None and comments_count > prev_comments_count:
        delta = comments_count - prev_comments_count
        latest = comments_list[-1] if comments_list else {}
        author = ((latest.get("author") or {}).get("login") if isinstance(latest, dict) else "") or "?"





        new_comments = comments_list[-delta:] if 0 < delta <= len(comments_list) else []
        events.append({
            "event": "comment_added",
            "payload": {
                "url": url,
                "title": title,
                "author": author,
                "author_is_viewer": _author_is_viewer(new_comments),
                "new_count": delta,
            },
            "notify_title": f"#{number} new comment{'s' if delta > 1 else ''}",
            "notify_message": f"by {author}: {title}",
        })


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
