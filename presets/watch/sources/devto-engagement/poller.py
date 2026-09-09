"""devto-engagement watcher source — comments, replies and reaction-threshold
crossings across your recently published articles (#526).

`devto_status_since` already does exactly this fetch, on demand: list
`/articles/me/published`, then `/comments?a_id=X` for each. It exists because
dev.to gives no push channel and something can land on any of your articles
at any time -- which is a watcher's job, not a command you remember to
re-run. This source is that fetch taught to run itself and to remember what
it already told you, as a population poller over a scope (`@me`, mirroring
`gitlab-mr-feed`/`github-issue-feed`) rather than a per-id watcher: the
interesting engagement is usually on whichever article is trending, not on
the one id somebody happened to spawn a watcher for.

    state       {aid: {title, url, reactions, comment_ids}} for the most
                recently published articles
    new comment, parent not one of your own comments   comment_received
    new comment whose parent you posted through this tool   reply_received
    public_reactions_count crosses a threshold in REACTION_THRESHOLDS
                reaction_received
    no answer   emit engagement_unreachable once, change nothing else
    terminal    never — engagement has no end state

Why reactions are threshold-crossings and not a bare count
------------------------------------------------------------
dev.to exposes reactions as one running integer per article; there is no
per-reaction feed the way a comment arrives with its own id and timestamp.
"12 more reactions since last tick" names a metric that moved, not a thing
that happened -- there is no before/after two people can point at and agree
"that is the event". #526 is explicit that a reaction total with no
nameable before/after must either become a threshold crossing or be left
out entirely, never a bare running-count event, so this source reports the
crossing: was under N, is now at or over N. A crossing already behind an
article when its baseline poll first sees it is not reported either, for the
same reason a feed's first poll never announces the population it opens on.

Replies are told from ordinary new comments by the local outbound ledger
(`presets/devto/_outbound.py`), the same one `devto_status_since` reads:
dev.to has no "comments by me" endpoint, so a comment counts as a reply only
when its parent is one this tool itself posted. A comment answering someone
else's comment on your own article is still reported, as `comment_received`
-- it is real engagement, it just did not happen to you.

Nothing here calls through `presets/devto/_auth.py::get_api_key` or
`presets/devto/_rest.py::request`: both call `sys.exit` on failure, which is
the right behaviour for a one-shot CLI command and the wrong one for a
poller a dispatcher expects to keep running -- a poller that exits on a
network blip is a watcher that silently stops covering anything, which is
this repository's own "an absence produced by the tool, read as an absence
in the world" defect. `_resolve_api_key` and `_get` below are the same
resolution order and the same request shape, rewritten to return `None`/an
error string instead.

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
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

_PRESETS_DIR = Path(__file__).parents[3]
_DEVTO_DIR = _PRESETS_DIR / "devto"
sys.path.insert(0, str(_PRESETS_DIR))
sys.path.insert(0, str(_DEVTO_DIR))

from _env import env_int  # noqa: E402  (the one numeric-knob reader, #654)
from _http import (  # noqa: E402
    DeadlineExceeded,
    RedirectRefused,
    ResponseTooLarge,
    read_capped,
    urlopen,
)
from _outbound import my_comment_ids, read as read_outbound  # noqa: E402

# Ten minutes. Each tick costs one `/articles/me/published` call plus one
# `/comments` call per recent article -- generous on purpose, since #526's
# real constraint is dev.to's rate limit rather than how fast a comment or a
# reaction needs to surface.
INTERVAL = 600

BASE = "https://dev.to/api"

DEFAULT_SCOPE = "@me"

# Bounded on purpose: one extra /comments call per article per tick, so this
# is also the ceiling on API traffic per poll. Same knob `devto_status_since`
# already reads (`SUPERTOOL_STATUS_POSTS`), so the two agree about what
# "recent" means for one account rather than each guessing its own number.
DEFAULT_MAX_ARTICLES = 10

REACTION_THRESHOLDS = (10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000)

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


def _resolve_api_key() -> str | None:
    """Mirrors `presets/devto/_auth.py::get_api_key`'s resolution order, but
    returns `None` instead of exiting -- see the module docstring for why a
    poller cannot afford `sys.exit` on a missing credential. That includes a
    credential *file* that cannot be read: a TOCTOU deletion between
    `is_file()` and `read_text()`, a permission error, or a non-UTF-8 byte in
    a hand-edited token file must fall through to the next candidate and
    finally to `None`, not raise past this function into the dispatcher's
    generic failure counter -- which reports nothing on the wire at all
    until it gives up on the whole watcher, far short of the immediate,
    edge-triggered `engagement_unreachable` a credential problem deserves
    (found in review; #526)."""
    val = os.environ.get("DEVTO_API_KEY", "").strip()
    if val:
        return val
    for p in ("~/.config/devto/token", ".devto-token"):
        path = Path(os.path.expanduser(p))
        try:
            if path.is_file():
                text = path.read_text(encoding="utf-8").strip()
                if text:
                    return text
        except (OSError, UnicodeDecodeError):
            continue
    return None


def _get(path: str, api_key: str, query: dict[str, Any] | None = None,
         timeout: int = 20) -> tuple[Any, str]:
    """`(data, "")` or `(None, why)`. Never raises, never exits."""
    url = f"{BASE}{path}"
    if query:
        clean = {k: v for k, v in query.items() if v is not None and v != ""}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
    headers = {
        "api-key": api_key,
        "Accept": "application/vnd.forem.api-v1+json",
        "User-Agent": ("claude-supertool/devto-engagement "
                       "(+https://github.com/Digital-Process-Tools/claude-supertool)"),
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urlopen(req, timeout=timeout) as resp:
            text = read_capped(resp).decode("utf-8")
    except RedirectRefused as e:
        return None, f"ERROR: {e}"
    except (ResponseTooLarge, DeadlineExceeded) as e:
        return None, f"ERROR: {e}"
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return None, "ERROR: 401 Unauthorized — check DEVTO_API_KEY, may have expired"
        return None, f"ERROR: HTTP {e.code} {e.reason}"
    except urllib.error.URLError as e:
        return None, f"ERROR: network: {e.reason}"
    except http.client.HTTPException as e:
        return None, f"ERROR: incomplete response: {type(e).__name__}: {e}"
    except ValueError as e:
        # UnicodeDecodeError subclasses ValueError, reached if a gateway
        # ever answers with a non-UTF-8 body. `http.client.InvalidURL` does
        # NOT reach this arm -- it subclasses `http.client.HTTPException`
        # (verified: `InvalidURL.__mro__`), so a query value carrying a
        # control character is already caught by the `HTTPException` arm
        # above, just under its "incomplete response" wording rather than
        # this one. `presets/devto/_rest.py::request`'s own comment claims
        # `InvalidURL` lands here too; it does not, in that function either
        # -- found while auditing this one (#526 second-pass review) and
        # left uncorrected there since that file is outside this diff.
        return None, f"ERROR: bad response: {e}"
    if not text:
        return {}, ""
    try:
        return json.loads(text), ""
    except json.JSONDecodeError:
        return None, "ERROR: invalid JSON from dev.to"


def fetch_population(scope: str, api_key: str) -> tuple[dict[str, dict[str, Any]] | None, str]:
    """`({aid: {title, url, reactions}}, "")`, or `(None, why)`.

    Only `@me` is understood so far -- the population every existing devto
    watcher (`devto_status_since`) already means by "recent activity".
    """
    if scope != DEFAULT_SCOPE:
        return None, (f"ERROR: scope {scope!r} is not supported — only "
                      f"{DEFAULT_SCOPE!r} (your own published articles)")
    max_articles = env_int("SUPERTOOL_STATUS_POSTS", DEFAULT_MAX_ARTICLES, minimum=1)
    data, error = _get("/articles/me/published", api_key,
                       query={"per_page": max_articles})
    if error:
        return None, error
    if not isinstance(data, list):
        return None, "ERROR: unexpected payload shape from dev.to"
    out: dict[str, dict[str, Any]] = {}
    for a in data:
        if not isinstance(a, dict) or a.get("id") is None:
            continue
        out[str(a["id"])] = {
            "title": str(a.get("title") or ""),
            "url": str(a.get("url") or ""),
            "reactions": int(a.get("public_reactions_count") or 0),
        }
    return out, ""


def fetch_comment_ids(aid: str, api_key: str) -> tuple[list[dict[str, str]] | None, str]:
    """Flat `[{id, parent}]` for every comment on an article, one level deep
    (dev.to nests one deep). `None` on any failure -- never a partial list,
    so a transient failure cannot read as "these comments went away"."""
    data, error = _get("/comments", api_key, query={"a_id": aid})
    if error:
        return None, error
    if not isinstance(data, list):
        return None, "ERROR: unexpected payload shape from dev.to"
    out: list[dict[str, str]] = []
    for c in data:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id_code") or "")
        if not cid:
            continue
        out.append({"id": cid, "parent": ""})
        for child in c.get("children") or []:
            if not isinstance(child, dict):
                continue
            child_id = str(child.get("id_code") or "")
            if child_id:
                out.append({"id": child_id, "parent": cid})
    return out, ""


def _crossings(prev: int, current: int) -> list[int]:
    return [t for t in REACTION_THRESHOLDS if prev < t <= current]


def poll(state: dict, ctx: dict) -> tuple[list[dict], dict]:
    scope = str(ctx.get("id") or DEFAULT_SCOPE)
    api_key = _resolve_api_key()

    if api_key is None:
        population, error = None, ("ERROR: Dev.to API key not found — set "
                                   "DEVTO_API_KEY, or write to "
                                   "~/.config/devto/token")
    else:
        population, error = fetch_population(scope, api_key)

    raw_known = state.get("known")
    known: dict[str, dict[str, Any]] = raw_known if isinstance(raw_known, dict) else {}

    if population is None:
        # Three answers, not two: ok, a finding, and cannot tell. Said once
        # per outage, not once per poll -- an alert every ten minutes for an
        # expired key is one people mute, and a muted alert is the original
        # silence by a longer route. `{**state, ...}` is the recovery
        # guarantee: `known` has to survive an outage untouched, or the
        # first successful poll after it re-announces the whole population.
        new_state = {**state, "lookup": LOOKUP_UNAVAILABLE, "error": error}
        if state.get("lookup") == LOOKUP_UNAVAILABLE:
            return [], new_state
        return [{
            "event": "engagement_unreachable",
            "payload": {"scope": scope, "error": error,
                       "last_known_count": len(known)},
            "notify_title": f"dev.to engagement {scope} — cannot tell",
            "notify_message": error,
        }], new_state

    my_ids = my_comment_ids(read_outbound())
    events: list[dict] = []
    new_known: dict[str, dict[str, Any]] = {}

    for aid, meta in population.items():
        prev = known.get(aid)
        baseline = prev is None
        prev_ids = set(prev.get("comment_ids") or []) if prev else set()
        prev_reactions = int(prev.get("reactions") or 0) if prev else 0

        comment_rows, _c_error = fetch_comment_ids(aid, api_key)
        if comment_rows is None:
            if baseline:
                # Never seen this article before, and could not read its
                # comments on this very tick either -- there is no known
                # comment set to carry forward. Recording `comment_ids: []`
                # here would lock in an empty baseline: the next successful
                # fetch would then diff the article's whole pre-existing
                # comment set against that false empty set and announce
                # every one of them as new, which is the exact false-arrival
                # a transient failure must never cause. Leaving the article
                # out of `new_known` keeps it baseline (silent) until a poll
                # actually establishes what was already there.
                continue
            # Already established once -- carry the previous ids forward
            # unchanged rather than guessing, so a transient failure never
            # manufactures a false arrival on the next successful tick.
            current_ids = prev_ids
            by_parent: dict[str, str] = {}
        else:
            current_ids = {r["id"] for r in comment_rows}
            by_parent = {r["id"]: r["parent"] for r in comment_rows}

        if not baseline:
            for cid in sorted(current_ids - prev_ids):
                parent = by_parent.get(cid, "")
                payload = {"article": aid, "url": meta["url"],
                          "title": meta["title"], "comment": cid}
                if parent and parent in my_ids:
                    events.append({
                        "event": "reply_received",
                        "payload": {**payload, "parent": parent},
                        "notify_title": f"reply on {meta['title']!r}",
                        "notify_message": f"comment {cid}",
                    })
                else:
                    events.append({
                        "event": "comment_received",
                        "payload": payload,
                        "notify_title": f"new comment on {meta['title']!r}",
                        "notify_message": f"comment {cid}",
                    })
            for t in _crossings(prev_reactions, meta["reactions"]):
                events.append({
                    "event": "reaction_received",
                    "payload": {"article": aid, "url": meta["url"],
                               "title": meta["title"], "threshold": t,
                               "reactions": meta["reactions"]},
                    "notify_title": f"{meta['title']!r} passed {t} reactions",
                    "notify_message": f"{meta['reactions']} reactions",
                })

        new_known[aid] = {
            "title": meta["title"],
            "url": meta["url"],
            "reactions": meta["reactions"],
            "comment_ids": sorted(current_ids),
        }

    return events, {"known": new_known, "lookup": LOOKUP_OK}


def is_terminal(state: dict) -> bool:
    """Never. Engagement has no end state, and a poller that stopped itself
    would restore the silence this source exists to remove."""
    return False
