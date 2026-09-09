"""bluesky-engagement watcher source — replies, mentions/quotes and
like/repost notifications (#526).

`bluesky_status_since` already fetches exactly this: the native
notifications endpoint, filtered to what is new since a timestamp -- because
Bluesky, like dev.to, gives no push channel of its own and something can
land at any time. This source is that fetch taught to run itself, as a
population poller over the notification feed (a scope, `@me`, mirroring
`devto-engagement`/`gitlab-mr-feed`) rather than a per-post watcher.

    state       {seen: [notification uri, ...]} — everything the endpoint
                has already reported
    reason=reply                 reply_received
    reason=mention or quote      comment_received
    reason=like or repost        reaction_received
    reason=follow                nothing (not engagement with a post)
    no answer                    emit engagement_unreachable once
    terminal                     never — engagement has no end state

Why a like is reported directly here, and not as a threshold crossing
------------------------------------------------------------------------
#526 asks that a reaction total with no nameable before/after either become
a threshold-crossing event or be left out entirely. `devto-engagement` (this
source's sibling) hits exactly that case: dev.to exposes one running
integer per article and no per-reaction feed. Bluesky's notification
endpoint is a different shape -- every like and repost arrives as its own
notification, with its own author, its own subject `uri` and its own
`indexedAt`, exactly like a reply does. "did @user like this post, at this
time" is as nameable a before/after as "did @user reply to this post", so
this source reports each one directly the moment it is new, rather than
reducing it to a count first and re-deriving a crossing from that count --
which would throw away identity the API already gives for free and invent
a threshold nobody asked for. `follow` is deliberately not mapped to any of
the three events #526 names: it is not engagement with a post, and forcing
it into `reaction_received` would misdescribe it.

Nothing here calls through `presets/bluesky/_auth.py` or
`presets/bluesky/_atproto.py`: both `sys.exit` on failure, correct for a
one-shot CLI command and wrong for a poller a dispatcher expects to keep
running. `_resolve_handle_and_password`, `get_session` and
`fetch_notifications` below are the same resolution order and the same
session lifecycle (create / refresh from the same cached session file),
rewritten to return `None`/an error string instead of exiting.

Source plugin contract:
- INTERVAL: int seconds between polls
- poll(state, ctx) -> (events, new_state)
- is_terminal(state) -> bool
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

_PRESETS_DIR = Path(__file__).parents[3]
sys.path.insert(0, str(_PRESETS_DIR))

from _http import (  # noqa: E402
    DeadlineExceeded,
    RedirectRefused,
    ResponseTooLarge,
    read_capped,
    urlopen,
)

# Five minutes. Replies and likes move faster than dev.to comments in
# practice, but the real constraint per #526 is Bluesky's rate limit, not
# how fast a like needs to surface -- generous on purpose.
INTERVAL = 300

PDS = "https://bsky.social"
SESSION_FILE = Path(os.path.expanduser("~/.config/bluesky/session.json"))

DEFAULT_SCOPE = "@me"

# Bounded so `seen` cannot grow without limit across a long-lived poller.
MAX_SEEN = 2000

NOTIFICATION_LIMIT = 50

# Reference table only -- kept for the reader, not for dispatch. `poll()`
# below branches on `reason` and writes each `"event"` key as a literal, so
# `tests/test_watch_bluesky_engagement_526.py`'s AST scan (mirroring
# `github-issue-feed`'s) can read the source as the one witness of what it
# emits, rather than trusting a dict this table and that scan could drift
# out of step with by hand.
#   reply            -> reply_received
#   mention, quote   -> comment_received
#   like, repost     -> reaction_received
#   follow           -> nothing (not engagement with a post)
REPLY_REASONS = {"reply"}
COMMENT_REASONS = {"mention", "quote"}
REACTION_REASONS = {"like", "repost"}

LOOKUP_OK = "ok"
LOOKUP_UNAVAILABLE = "unavailable"

# What this source can put on the wire. `events.json` is asserted equal to
# it: a declared key nothing emits is an untrue claim, and an emitted key
# nothing declares cannot be named in `only=`.
EVENT_KEYS = (
    "comment_received",
    "reply_received",
    "reaction_received",
    "engagement_unreachable",
)


def _resolve_handle_and_password() -> tuple[str, str] | None:
    """Mirrors `presets/bluesky/_auth.py`'s resolution order, but returns
    `None` instead of exiting — see the module docstring for why. That
    includes a credential *file* that cannot be read: a TOCTOU deletion, a
    permission error, or a non-UTF-8 byte in a hand-edited file must fall
    through to the next candidate rather than raise past this function into
    the dispatcher's generic failure counter, which reports nothing on the
    wire until it gives up on the whole watcher -- far short of the
    immediate, edge-triggered `engagement_unreachable` a credential problem
    deserves (found in review; #526)."""
    handle = os.environ.get("BLUESKY_HANDLE", "").strip()
    if not handle:
        for p in ("~/.config/bluesky/handle", ".bluesky-handle"):
            path = Path(os.path.expanduser(p))
            try:
                if path.is_file():
                    handle = path.read_text(encoding="utf-8").strip()
                    if handle:
                        break
            except (OSError, UnicodeDecodeError):
                continue
    password = os.environ.get("BLUESKY_APP_PASSWORD", "").strip()
    if not password:
        for p in ("~/.config/bluesky/app_password", ".bluesky-app-password"):
            path = Path(os.path.expanduser(p))
            try:
                if path.is_file():
                    password = path.read_text(encoding="utf-8").strip()
                    if password:
                        break
            except (OSError, UnicodeDecodeError):
                continue
    if not handle or not password:
        return None
    return handle, password


def _call(url: str, headers: dict[str, str], body: dict | None,
          timeout: int) -> tuple[dict[str, Any] | None, str]:
    """`(data, "")` or `(None, why)`. Never raises, never exits."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    call_headers = dict(headers)
    method = "GET"
    if data is not None:
        call_headers["Content-Type"] = "application/json"
        method = "POST"
    req = urllib.request.Request(url, data=data, headers=call_headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as resp:
            text = read_capped(resp).decode("utf-8")
    except RedirectRefused as e:
        return None, f"ERROR: {e}"
    except (ResponseTooLarge, DeadlineExceeded) as e:
        return None, f"ERROR: {e}"
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return None, "ERROR: 401 Unauthorized — session expired or app password wrong"
        return None, f"ERROR: HTTP {e.code} {e.reason}"
    except urllib.error.URLError as e:
        return None, f"ERROR: network: {e.reason}"
    except http.client.HTTPException as e:
        return None, f"ERROR: incomplete response: {type(e).__name__}: {e}"
    except ValueError as e:
        # UnicodeDecodeError subclasses ValueError, reached if a gateway
        # ever answers with a non-UTF-8 body. `http.client.InvalidURL` does
        # NOT reach this arm -- it subclasses `http.client.HTTPException`
        # (verified: `InvalidURL.__mro__`), so a URL component carrying a
        # control character is already caught by the `HTTPException` arm
        # above, just under its "incomplete response" wording rather than
        # this one -- found while auditing the sibling `devto-engagement`
        # source's identical comment against the real MRO (#526 second-pass
        # review).
        return None, f"ERROR: bad response: {e}"
    try:
        return json.loads(text), ""
    except json.JSONDecodeError:
        return None, "ERROR: invalid JSON from Bluesky"


def _load_session() -> dict[str, Any] | None:
    try:
        return json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _save_session(session: dict[str, Any]) -> None:
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps(session), encoding="utf-8")
    try:
        os.chmod(SESSION_FILE, 0o600)
    except OSError:
        pass


def _create_session(handle: str, password: str) -> tuple[dict[str, Any] | None, str]:
    data, error = _call(f"{PDS}/xrpc/com.atproto.server.createSession", {},
                        {"identifier": handle, "password": password}, 20)
    if data is None:
        return None, error
    data["_created_at"] = int(time.time())
    _save_session(data)
    return data, ""


def _refresh_session(refresh_jwt: str) -> dict[str, Any] | None:
    data, _error = _call(f"{PDS}/xrpc/com.atproto.server.refreshSession",
                         {"Authorization": f"Bearer {refresh_jwt}"}, None, 20)
    if data is None:
        return None
    data["_created_at"] = int(time.time())
    _save_session(data)
    return data


def get_session(handle: str, password: str) -> tuple[dict[str, Any] | None, str]:
    session = _load_session()
    if session and session.get("handle") == handle:
        age = int(time.time()) - session.get("_created_at", 0)
        if age < 5400:  # ~2h token lifetime; refresh past 1.5h
            return session, ""
        refreshed = _refresh_session(session.get("refreshJwt", ""))
        if refreshed:
            return refreshed, ""
    return _create_session(handle, password)


def fetch_notifications(scope: str) -> tuple[list[dict[str, Any]] | None, str]:
    """`([notification, ...], "")`, or `(None, why)`.

    Only `@me` is understood so far -- the population every existing
    bluesky watcher (`bluesky_status_since`) already means by "my
    notifications".
    """
    if scope != DEFAULT_SCOPE:
        return None, (f"ERROR: scope {scope!r} is not supported — only "
                      f"{DEFAULT_SCOPE!r} (your own notifications)")
    creds = _resolve_handle_and_password()
    if creds is None:
        return None, ("ERROR: Bluesky handle/app password not found — set "
                      "BLUESKY_HANDLE and BLUESKY_APP_PASSWORD")
    handle, password = creds
    session, error = get_session(handle, password)
    if session is None:
        return None, error or "ERROR: could not create a Bluesky session"
    url = (f"{PDS}/xrpc/app.bsky.notification.listNotifications?"
          f"{urllib.parse.urlencode({'limit': NOTIFICATION_LIMIT})}")
    data, error = _call(url, {"Authorization": f"Bearer {session.get('accessJwt', '')}"},
                        None, 20)
    if data is None:
        return None, error
    items = data.get("notifications")
    if not isinstance(items, list):
        return None, "ERROR: unexpected payload shape from Bluesky"
    return items, ""


def poll(state: dict, ctx: dict) -> tuple[list[dict], dict]:
    scope = str(ctx.get("id") or DEFAULT_SCOPE)
    notifications, error = fetch_notifications(scope)

    raw_seen = state.get("seen")
    seen: list[str] = raw_seen if isinstance(raw_seen, list) else []
    seen_set = set(seen)
    # `baselined` survives an outage in `{**state, ...}` below, so a source
    # that has genuinely observed the population once never re-baselines
    # after a later failure -- only a source that has NEVER seen it does.
    baseline = not state.get("baselined")

    if notifications is None:
        new_state = {**state, "lookup": LOOKUP_UNAVAILABLE, "error": error}
        if state.get("lookup") == LOOKUP_UNAVAILABLE:
            return [], new_state
        return [{
            "event": "engagement_unreachable",
            "payload": {"scope": scope, "error": error,
                       "last_known_count": len(seen)},
            "notify_title": f"Bluesky engagement {scope} — cannot tell",
            "notify_message": error,
        }], new_state

    events: list[dict] = []
    new_uris: list[str] = []
    for n in notifications:
        if not isinstance(n, dict):
            continue
        uri = str(n.get("uri") or "")
        if not uri or uri in seen_set:
            continue
        new_uris.append(uri)
        reason = str(n.get("reason") or "")
        if baseline or (reason not in REPLY_REASONS
                        and reason not in COMMENT_REASONS
                        and reason not in REACTION_REASONS):
            continue
        author = n.get("author") or {}
        record = n.get("record") or {}
        text = str(record.get("text") or "").replace("\n", " ")[:160]
        payload = {
            "uri": uri,
            "reason": reason,
            "author": str(author.get("handle") or ""),
            "subject": str(n.get("reasonSubject") or ""),
            "text": text,
        }
        notify = {
            "notify_title": f"@{payload['author']} {reason}",
            "notify_message": text,
        }
        if reason in REPLY_REASONS:
            events.append({"event": "reply_received", "payload": payload, **notify})
        elif reason in COMMENT_REASONS:
            events.append({"event": "comment_received", "payload": payload, **notify})
        else:
            events.append({"event": "reaction_received", "payload": payload, **notify})

    updated = seen + [u for u in new_uris if u not in seen_set]
    if len(updated) > MAX_SEEN:
        updated = updated[-MAX_SEEN:]

    return events, {"seen": updated, "baselined": True, "lookup": LOOKUP_OK}


def is_terminal(state: dict) -> bool:
    """Never. Engagement has no end state, and a poller that stopped itself
    would restore the silence this source exists to remove."""
    return False
