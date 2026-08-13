"""github-issue-feed watcher source — GitHub issues, population-first.

#525 asked for `watch:github-issue:<n>`, a poller over one known number. Its
own comment then withdrew that shape, and the comment is right: the motivating
case is a workflow keyed off a *label*, and the label arrives on issues nobody
spawned a poller for. "Was an issue created?" is unanswerable by construction
from a watcher over a number that already exists. So this is a feed, and there
is deliberately no per-id companion.

The reason no companion is needed is specific to issues rather than a general
preference. `gitlab-mr-feed` has to spawn a per-MR poller because the facts
worth watching on an MR — pipeline status, conflicts, approvals — are not in
the list payload. Every fact #525 named *is*: one
`GET /repos/{owner}/{repo}/issues` page carries `labels`, `assignees`,
`comments` (an integer), `state_reason` and `created_at` for the whole
population. So this source has one tier, no fan-out, no cross-tier duplicate
suppression, and one API call per poll whatever the population size.

    state       {number: {title, url, labels, assignees, comments, ...}}
    new number  issue_opened / issue_reopened / issue_entered_feed
    same number diff labels, assignees, comment count
    gone number look it up, then issue_closed / issue_left_feed
    terminal    never — discovery has no end state

Three traps this file exists to not fall into a second time:

1. **REST `/issues` returns pull requests in the same array.** 79 rows on this
   repository, 2 of them PRs. A PR entering the population fires `issue_opened`
   for every pull request anyone raises. Rows carrying a `pull_request` key are
   dropped.
2. **A truncated page run is not a short population.** Returning what fits
   under the page cap fires a departure for every issue past it — the tool's
   own absence read as the world's, this repository's house defect. Truncation
   returns `None` with a reason, exactly like a 401.
3. **`comments` is a human-comment count, not an activity count.** The GitLab
   side of this question (`user_notes_count`) cost two filed issues while
   everyone believed the opposite; the GitHub field counts issue comments only,
   so a label edit does not move it and `issue_comment_added` does not fire
   with nothing to read.

Source plugin contract:
- INTERVAL: int seconds between polls
- poll(state, ctx) -> (events, new_state)
- is_terminal(state) -> bool
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import time
import urllib.parse
from pathlib import Path
from typing import Any

# Labels move on human timescales, but a label-triggered handoff is the case
# #525 names, and a handoff that waits out `gitlab-mr-feed`'s 300s is the
# friction being fixed rather than half of it. One `gh api` call per tick is 30
# requests an hour against a 5000/hour budget.
INTERVAL = 120

PER_PAGE = 100

# 500 issues. Past that the population cannot be established in bounded time,
# and this source says so rather than reporting a prefix of it (see trap 2).
MAX_PAGES = 5

# A closed number that comes back is the one arrival this source can classify
# rather than infer, and the window is bounded so long-lived state cannot grow
# without limit. Beyond it an arrival degrades to `issue_entered_feed`, which
# is the honest answer and not a worse-looking one.
MAX_CLOSED_RECENT = 500

DEFAULT_SCOPE = "@open"

ALIASES = {
    "@open": "state=open",
}

# What this source can put on the wire. `events.json` is asserted equal to it,
# because a declared key nothing emits is an untrue claim and an emitted key
# nothing declares cannot be named in `only=`.
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

# REST query parameters this source will pass through. An allow-list rather
# than a pass-through: a token GitHub ignores widens the population silently,
# and a wider population than the caller asked for announces strangers' issues
# (#939, on the GitLab side of the same shape).
FILTER_KEYS = {"state", "assignee", "creator", "mentioned", "milestone",
               "sort", "direction", "labels"}

# `labels` is comma-joined by REST and the scope separator is also a comma, so
# `label=` repeated is the only unambiguous spelling of "and".
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


def resolve_filters(scope: str) -> dict[str, str] | None:
    """REST query parameters for a scope string. `None` when a token is unknown.

    `None` is not an empty dict. An empty dict is a legitimate scope (every
    issue GitHub will return); `None` is "this scope was not understood", and
    the two must not resolve the same way — building the query anyway drops the
    token and therefore *widens* the population past what was asked for.
    """
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
        # A `labels=` written out longhand *and* repeated `label=` tokens is
        # two answers to one question; the longhand one wins nothing, so say so
        # rather than silently picking.
        if "labels" in out:
            return None
        out["labels"] = ",".join(labels)
    return out


def _unknown_token(scope: str) -> str:
    """The first token `resolve_filters` could not apply, for the message.

    Recomputed rather than returned alongside `None`, so the refusal stays one
    value with one meaning. Naming the token is the difference between a
    refusal the operator can act on and one that sends them to the source.
    """
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
        # `None`, never 0, when the field is missing: the rising-edge guard
        # treats it as "no baseline" rather than locking the count at zero.
        "comments": raw_comments if isinstance(raw_comments, int) else None,
        "created_at": str(item.get("created_at") or ""),
        "state_reason": str(item.get("state_reason") or ""),
    }


def fetch_population(scope: str) -> tuple[dict[str, dict[str, Any]] | None, str]:
    """`({number: row}, "")`, or `(None, why)` when the population is unknown.

    Never a partial answer. Every failure — an unknown scope token, a gh
    failure, junk JSON, more pages than the cap — returns `None`, because a
    short population is indistinguishable from issues that closed and would
    fire a departure event for each one.
    """
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
    for page in range(1, MAX_PAGES + 1):
        query["page"] = str(page)
        path = _repo_target.api_path("issues") + "?" + urllib.parse.urlencode(query)
        try:
            result = _gh(["api", path])
        except FileNotFoundError:
            return None, "ERROR: gh not found — install from https://cli.github.com"
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
        for item in data:
            if not isinstance(item, dict) or item.get("number") is None:
                continue
            # REST serves issues and pull requests from one endpoint and tells
            # them apart only by this key. Without the drop, every PR anyone
            # opens arrives as an issue.
            if "pull_request" in item:
                continue
            out.setdefault(str(item["number"]), _issue_row(item))
        if len(data) < PER_PAGE:
            return out, ""
    return None, (f"ERROR: scope {scope!r} returned more than "
                  f"{PER_PAGE * MAX_PAGES} rows, so the population was not "
                  f"established — narrow it with a label or milestone filter")


def lookup_issue_state(number: str) -> str:
    """Live `state` of one issue — `""` when it could not be read.

    A number leaving `state=open` could have closed, been transferred, been
    relabelled out of the filter, or the filter could have changed. Guessing
    "closed" is right most of the time and confidently wrong the rest, so one
    extra call buys the truth. Departures are rare, so the call is too.
    """
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
    """The three fields every event carries.

    Flat strings, because `notifiers/claude-channel` renders each payload key
    as an XML attribute via `String(v)` — a list or a nested object arrives as
    `[object Object]`, i.e. invisible on the surface this source exists for.
    """
    return {
        "number": number,
        "title": str(row.get("title") or f"issue #{number}"),
        "url": str(row.get("url") or ""),
    }


def _set_delta(before: list[str], after: list[str]) -> tuple[list[str], list[str]]:
    """(added, removed), order taken from the side each came from.

    Set difference, not a length comparison: GitHub does not promise an order,
    and one label swapped for another leaves the count unmoved. A count-based
    check would report neither, which is the silence this source is for.
    """
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
                        # `+x,+y` rather than a bare list: "labels changed"
                        # sends the reader back to the API for the one fact
                        # they needed (#525's own wording).
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
    """The event for a number that was not in the population last poll.

    Three answers, not one. "Opened" is only claimed when the feed can
    establish the issue did not exist at the previous look; a number the feed
    itself watched close is a reopen it observed rather than inferred; and
    everything else is `issue_entered_feed`, which reports the arrival without
    asserting a cause the population query cannot support.
    """
    payload = _base_payload(number, row)
    created = str(row.get("created_at") or "")
    if number in closed_recent:
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
                    # Carried, not interpreted. GitHub leaves `reopened` on the
                    # issue forever, so it says the issue was reopened at some
                    # point — not that reopening is what brought it in now.
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
    # Rising edge only. A deleted comment lowers the count and nothing arrived.
    # `None` on either side is "no baseline", not zero.
    if (isinstance(prev_comments, int) and isinstance(comments, int)
            and comments > prev_comments):
        events.append({
            "event": "issue_comment_added",
            "payload": {**_base_payload(number, after),
                        "new_count": comments - prev_comments,
                        "comments": comments},
            "notify_title": f"#{number} new comment"
                            f"{'s' if comments - prev_comments > 1 else ''}",
            "notify_message": str(after.get("title") or ""),
        })
    return events


def _departure(number: str, row: dict[str, Any]) -> tuple[dict, bool]:
    """(event, closed) for a number that left the population."""
    payload = _base_payload(number, row)
    state = lookup_issue_state(number)
    if state == "closed":
        return {
            "event": "issue_closed",
            "payload": payload,
            "notify_title": f"#{number} closed",
            "notify_message": payload["title"],
        }, True
    # Still open, or unreadable. Leaving *this filter* is not the same claim as
    # the issue ending, and the two must not render alike.
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
        # Three answers, not two: ok, a finding, and *cannot tell*. Said once
        # per outage rather than once per poll — an alert that repeats every
        # two minutes is one people mute, and a muted alert is the original
        # silence by a longer route.
        #
        # `{**state, ...}` is the recovery guarantee: `known`, `observed_at`
        # and `closed_recent` all have to survive untouched, or the first
        # successful poll after the outage re-announces the whole population.
        new_state = {**state, "lookup": LOOKUP_UNAVAILABLE, "error": error}
        if state.get("lookup") == LOOKUP_UNAVAILABLE:
            return [], new_state
        return [{
            "event": "issues_unreachable",
            "payload": {
                "scope": scope,
                "error": error,
                # `last_known_`, not the bare name: what we could see the last
                # time we could see, not what the tracker holds now.
                "last_known_count": len(known),
                "last_known_at": str(state.get("observed_at") or ""),
            },
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
        # The instant this population was read. `issue_opened` is decided
        # against it, so it is an absolute stamp rather than an age: an age is
        # correct for one second and quietly wrong afterwards.
        "observed_at": _now(),
        "closed_recent": _prune_closed(closed_recent),
        "lookup": LOOKUP_OK,
    }


def is_terminal(state: dict) -> bool:
    """Never. A population has no final state, and a feed that ends is the
    discovery gap back with nobody watching for it."""
    return False
