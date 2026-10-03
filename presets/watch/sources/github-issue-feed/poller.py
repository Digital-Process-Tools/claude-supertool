












































from __future__ import annotations

import importlib.util
import json
import subprocess
import time
import urllib.parse
from pathlib import Path
from typing import Any











INTERVAL = 120

PER_PAGE = 100



MAX_PAGES = 5





MAX_CLOSED_RECENT = 500

DEFAULT_SCOPE = "@open"

ALIASES = {
    "@open": "state=open",
}




EVENT_KEYS = (
    "issue_opened",
    "issue_reopened",
    "issue_entered_feed",
    "issue_labeled",
    "issue_unlabeled",
    "issue_assigned",
    "issue_unassigned",
    "issue_comment_added",
    "issue_closed",
    "issue_left_feed",
    "issues_unreachable",
)





FILTER_KEYS = {"state", "assignee", "creator", "mentioned", "milestone",
               "sort", "direction", "labels"}



REPEATED_KEY = "label"

LOOKUP_OK = "ok"
LOOKUP_UNAVAILABLE = "unavailable"

_GITHUB_DIR = Path(__file__).parents[3] / "github"
_PRESETS_DIR = Path(__file__).parents[3]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_gh = _load("gh_issue_feed_pr_op", _GITHUB_DIR / "pr.py")._gh
_format_error = _load("gh_issue_feed_run_op", _GITHUB_DIR / "run.py")._format_error
_repo_target = _load("gh_issue_feed_repo_target", _PRESETS_DIR / "_repo_target.py")
_WATCH_DIR = Path(__file__).parents[2]



ratelimit = _load("gh_issue_feed_ratelimit", _WATCH_DIR / "ratelimit.py")


def resolve_filters(scope: str) -> dict[str, str] | None:







    resolved = ALIASES.get(scope, scope)
    out: dict[str, str] = {}
    labels: list[str] = []
    for token in resolved.split(","):
        token = token.strip()
        if not token:
            continue
        key, sep, value = token.partition("=")
        if not sep:
            return None
        key, value = key.strip(), value.strip()
        if key == REPEATED_KEY:
            labels.append(value)
            continue
        if key not in FILTER_KEYS:
            return None
        out[key] = value
    if labels:



        if "labels" in out:
            return None
        out["labels"] = ",".join(labels)
    return out


def _unknown_token(scope: str) -> str:






    resolved = ALIASES.get(scope, scope)
    for token in resolved.split(","):
        token = token.strip()
        if not token:
            continue
        key, sep, _value = token.partition("=")
        if not sep:
            return token
        if key.strip() not in FILTER_KEYS and key.strip() != REPEATED_KEY:
            return key.strip()
    return scope


def _issue_row(item: dict[str, Any]) -> dict[str, Any]:
    labels = [str(l.get("name") or "") for l in item.get("labels") or []
              if isinstance(l, dict)]
    assignees = [str(a.get("login") or "") for a in item.get("assignees") or []
                 if isinstance(a, dict)]
    raw_comments = item.get("comments")
    return {
        "title": str(item.get("title") or ""),
        "url": str(item.get("html_url") or ""),
        "labels": [l for l in labels if l],
        "assignees": [a for a in assignees if a],


        "comments": raw_comments if isinstance(raw_comments, int) else None,
        "created_at": str(item.get("created_at") or ""),
        "state_reason": str(item.get("state_reason") or ""),




        "state": str(item.get("state") or ""),
    }


def fetch_population(scope: str) -> tuple[dict[str, dict[str, Any]] | None, str]:







    filters = resolve_filters(scope)
    if filters is None:
        return None, (f"ERROR: scope {scope!r} carries a token this source "
                      f"cannot apply ({_unknown_token(scope)!r}), so the "
                      f"population was not established. Known filters: "
                      f"{', '.join(sorted(FILTER_KEYS | {REPEATED_KEY}))}")
    query = dict(filters)
    query.setdefault("state", "open")
    query["per_page"] = str(PER_PAGE)

    out: dict[str, dict[str, Any]] = {}





    for page in range(1, MAX_PAGES + 2):
        query["page"] = str(page)
        path = _repo_target.api_path("issues") + "?" + urllib.parse.urlencode(query)
        try:
            result = _gh(["api", path])
        except FileNotFoundError:
            return None, "ERROR: gh not found — install the GitHub CLI"
        except subprocess.TimeoutExpired:
            return None, f"ERROR: gh timed out listing issues for scope {scope!r}"
        except (OSError, subprocess.SubprocessError) as err:
            return None, f"ERROR: gh could not run for scope {scope!r}: {err}"
        if result.returncode != 0:
            return None, _format_error(result.stderr or "", "Issue list", scope)
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None, f"ERROR: invalid JSON from gh for scope {scope!r}"
        if not isinstance(data, list):
            return None, f"ERROR: unexpected payload shape from gh for scope {scope!r}"
        if page > MAX_PAGES:
            if not data:
                return out, ""
            break
        for item in data:
            if not isinstance(item, dict) or item.get("number") is None:
                continue



            if "pull_request" in item:
                continue
            out.setdefault(str(item["number"]), _issue_row(item))
        if len(data) < PER_PAGE:
            return out, ""
    return None, (f"ERROR: scope {scope!r} returned more than "
                  f"{PER_PAGE * MAX_PAGES} rows, so the population was not "
                  f"established — narrow it with a label or milestone filter")


def lookup_issue_state(number: str) -> str:







    path = _repo_target.api_path(f"issues/{number}")
    try:
        result = _gh(["api", path])
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("state") or "")


def _base_payload(number: str, row: dict[str, Any]) -> dict[str, Any]:






    return {
        "number": number,
        "title": str(row.get("title") or f"issue #{number}"),
        "url": str(row.get("url") or ""),
    }


def _set_delta(before: list[str], after: list[str]) -> tuple[list[str], list[str]]:






    prev, cur = set(before), set(after)
    return ([x for x in after if x not in prev],
            [x for x in before if x not in cur])


def _membership_events(number: str, row: dict[str, Any],
                       added: list[str], removed: list[str],
                       add_key: str, remove_key: str, field: str,
                       noun: str) -> list[dict]:
    events: list[dict] = []
    current = ",".join(row.get(field) or [])
    if added:
        events.append({
            "event": add_key,
            "payload": {**_base_payload(number, row),
                        "added": ",".join(added),



                        "changed": ",".join(f"+{x}" for x in added),
                        field: current},
            "notify_title": f"#{number} +{added[0]}" if len(added) == 1
                            else f"#{number} +{len(added)} {noun}",
            "notify_message": str(row.get("title") or ""),
        })
    if removed:
        events.append({
            "event": remove_key,
            "payload": {**_base_payload(number, row),
                        "removed": ",".join(removed),
                        "changed": ",".join(f"-{x}" for x in removed),
                        field: current},
        })
    return events


def _arrival(number: str, row: dict[str, Any], prev_observed: str,
             closed_recent: dict[str, Any]) -> dict:








    payload = _base_payload(number, row)
    created = str(row.get("created_at") or "")






    if number in closed_recent and _is_open(row):
        return {
            "event": "issue_reopened",
            "payload": {**payload, "closed_seen_at": str(closed_recent[number])},
            "notify_title": f"#{number} reopened",
            "notify_message": payload["title"],
        }
    if prev_observed and created and created > prev_observed:
        return {
            "event": "issue_opened",
            "payload": {**payload, "created_at": created,
                        "labels": ",".join(row.get("labels") or [])},
            "notify_title": f"#{number} opened",
            "notify_message": payload["title"],
        }
    return {
        "event": "issue_entered_feed",
        "payload": {**payload, "created_at": created,
                    "labels": ",".join(row.get("labels") or []),



                    "state_reason": str(row.get("state_reason") or "")},
        "notify_title": f"#{number} entered the feed",
        "notify_message": payload["title"],
    }


def _changes(number: str, before: dict[str, Any], after: dict[str, Any]) -> list[dict]:
    events: list[dict] = []
    added, removed = _set_delta(before.get("labels") or [], after.get("labels") or [])
    events += _membership_events(number, after, added, removed,
                                 "issue_labeled", "issue_unlabeled",
                                 "labels", "labels")
    added, removed = _set_delta(before.get("assignees") or [],
                                after.get("assignees") or [])
    events += _membership_events(number, after, added, removed,
                                 "issue_assigned", "issue_unassigned",
                                 "assignees", "assignees")
    prev_comments = before.get("comments")
    comments = after.get("comments")


    if (isinstance(prev_comments, int) and isinstance(comments, int)
            and comments > prev_comments):
        events.append({
            "event": "issue_comment_added",
            "payload": {**_base_payload(number, after),










                        "author_is_viewer": "unknown",
                        "new_count": comments - prev_comments,
                        "comments": comments},
            "notify_title": f"#{number} new comment"
                            f"{'s' if comments - prev_comments > 1 else ''}",
            "notify_message": str(after.get("title") or ""),
        })
    return events


def _is_open(row: dict[str, Any]) -> bool:







    return str(row.get("state") or "") == "open"


def _departure(number: str, row: dict[str, Any]) -> tuple[dict, bool]:









    payload = _base_payload(number, row)
    was_open = _is_open(row)
    state = lookup_issue_state(number)
    if state == "closed" and was_open:
        return {
            "event": "issue_closed",
            "payload": payload,
            "notify_title": f"#{number} closed",
            "notify_message": payload["title"],
        }, True


    return {
        "event": "issue_left_feed",
        "payload": {**payload, "issue_state": state or "unknown"},
        "notify_title": f"#{number} left the feed",
        "notify_message": payload["title"],
    }, False


def _prune_closed(closed: dict[str, Any]) -> dict[str, Any]:
    if len(closed) <= MAX_CLOSED_RECENT:
        return closed
    ordered = sorted(closed.items(), key=lambda kv: str(kv[1]))
    return dict(ordered[-MAX_CLOSED_RECENT:])


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def poll(state: dict, ctx: dict) -> tuple[list[dict], dict]:
    scope = str(ctx.get("id") or DEFAULT_SCOPE)
    current, error = fetch_population(scope)

    raw_known = state.get("known")
    baseline = not isinstance(raw_known, dict)
    known: dict[str, dict[str, Any]] = raw_known if isinstance(raw_known, dict) else {}
    raw_closed = state.get("closed_recent")
    closed_recent: dict[str, Any] = raw_closed if isinstance(raw_closed, dict) else {}

    if current is None:











        extra = ratelimit.unreachable_extra(error)
        new_state = {**state, "lookup": LOOKUP_UNAVAILABLE, "error": error, **extra}
        if state.get("lookup") == LOOKUP_UNAVAILABLE:
            return [], new_state
        payload = {
            "scope": scope,
            "error": error,


            "last_known_count": len(known),
            "last_known_at": str(state.get("observed_at") or ""),
        }
        if "retry_after" in extra:
            payload["retry_after"] = extra["retry_after"]
        return [{
            "event": "issues_unreachable",
            "payload": payload,
            "notify_title": f"issue feed {scope} — cannot tell",
            "notify_message": error,
        }], new_state

    events: list[dict] = []
    prev_observed = str(state.get("observed_at") or "")
    if not baseline:
        for number, row in current.items():
            if number in known:
                events += _changes(number, known[number], row)
            else:
                events.append(_arrival(number, row, prev_observed, closed_recent))
                closed_recent.pop(number, None)
        for number, row in known.items():
            if number not in current:
                event, closed = _departure(number, row)
                events.append(event)
                if closed:
                    closed_recent[number] = _now()

    return events, {
        "scope": scope,
        "known": current,



        "observed_at": _now(),
        "closed_recent": _prune_closed(closed_recent),
        "lookup": LOOKUP_OK,
    }


def is_terminal(state: dict) -> bool:


    return False
