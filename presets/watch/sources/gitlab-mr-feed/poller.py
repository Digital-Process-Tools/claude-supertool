"""gitlab-mr-feed watcher source — continuous discovery of new MRs.

Every other source polls one known id. Nothing in that loop can discover an
id that did not exist when the poller was spawned, so a running watch session
is structurally blind to every MR opened after it started, and stays blind
until a human re-runs `radar`. Between two runs the board is not wrong-looking
— it is confidently complete and missing an MR, which renders identically to
all-green.

This source polls the *population* instead of one member of it:

    state       {iid: {title, web_url}} for everything the filter returns
    new iid     spawn watch:gitlab-mr:<iid>, emit mr_opened
    gone iid    look the MR up, then emit mr_merged / mr_closed / mr_left_feed
    terminal    never — discovery has no end state

The watcher id is a *scope*, either an alias below or a literal `gl-mrs`
filter string, so the caller chooses the population (`@me` for what you wrote,
`@reviewer` for what you owe a review).

First poll records the baseline silently. Announcing every MR that was already
open when the feed started is not discovery, it is a notification storm.
Watchers are still spawned on that first poll, and on every poll after it, for
any id lacking a live poller — coverage is continuous for the same reason
discovery is.

Source plugin contract:
- INTERVAL: int seconds between polls
- poll(state, ctx) -> (events, new_state)
- is_terminal(state) -> bool
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

# MRs open on human timescales, not CI timescales. Five minutes bounds the
# discovery gap to something no one notices while costing one `glab mr list`
# per interval — the per-MR pollers already carry the 30s traffic.
INTERVAL = 300

ALIASES = {
    "@me": "author=@me,state=opened",
    "@reviewer": "reviewer=@me,state=opened",
}

_WATCH_DIR = Path(__file__).parents[2]
_PRESETS_DIR = Path(__file__).parents[3]


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mrs = _load("feed_gitlab_mrs", _PRESETS_DIR / "gitlab" / "mrs.py")
mr_op = _load("feed_gitlab_mr", _PRESETS_DIR / "gitlab" / "mr.py")
defaults = _load("feed_watch_defaults", _WATCH_DIR / "defaults.py")

_dispatcher_module: ModuleType | None = None


def _dispatcher() -> ModuleType:
    """Lazy — the dispatcher imports this module, so binding at import time
    would make each source load a second copy of its own loader."""
    global _dispatcher_module
    if _dispatcher_module is None:
        _dispatcher_module = _load("feed_watch_dispatcher", _WATCH_DIR / "dispatcher.py")
    return _dispatcher_module


def resolve_filter(scope: str) -> str:
    return ALIASES.get(scope, scope)


def fetch_population(scope: str) -> dict[str, dict[str, str]] | None:
    """{iid: {title, web_url}} for the scope's filter. None on any failure.

    None is deliberately not an empty dict: an unreachable GitLab must never
    read as "every MR you had is gone", which would fire a departure event for
    each of them.

    No pipeline enrichment — the feed answers "which MRs exist", and the
    per-MR watcher it spawns answers "what is happening to this one". One
    `glab mr list` call per poll, whatever the population size.
    """
    filters, _flags = mrs._parse_args(resolve_filter(scope))
    cfg = mrs._get_config()
    try:
        result = mrs._run(mrs._build_list_cmd(filters, cfg["per_page"]))
    except (OSError, ValueError):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    out: dict[str, dict[str, str]] = {}
    for m in data:
        if not isinstance(m, dict) or m.get("iid") is None:
            continue
        out[str(m["iid"])] = {
            "title": str(m.get("title") or ""),
            "web_url": str(m.get("web_url") or ""),
        }
    return out


def lookup_mr_state(iid: str) -> str:
    """Live `state` of one MR — "" when it cannot be read.

    An iid vanishing from `author=@me,state=opened` means merged, closed,
    reassigned, or a changed filter. Guessing "merged" is right most of the
    time and confidently wrong the rest, so one extra call buys the truth.
    Departures are rare, so the call is too.
    """
    try:
        r = mr_op._glab_api(f"projects/:id/merge_requests/{iid}")
    except (FileNotFoundError, OSError):
        return ""
    if r.returncode != 0:
        return ""
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("state") or "")


def live_watchers() -> set[str]:
    return mrs._watched_iids()


def spawn_watcher(iid: str) -> bool:
    only = [e for e in defaults.DEFAULT_ONLY.split(",") if e]
    try:
        return bool(_dispatcher()._spawn_poller(defaults.DEFAULT_SOURCE, iid, only))
    except OSError:
        return False


def stop_watcher(iid: str) -> None:
    try:
        _dispatcher().cmd_unwatch([defaults.DEFAULT_SOURCE, iid])
    except OSError:
        return


def _departure(iid: str, meta: dict[str, str]) -> dict[str, Any]:
    title = meta.get("title") or f"MR !{iid}"
    url = meta.get("web_url") or ""
    payload = {"iid": iid, "url": url, "title": title}
    state = lookup_mr_state(iid)
    if state in ("merged", "closed"):
        # The per-MR watcher reaches the same conclusion within 30s, emits its
        # own merged/closed and exits. This unwatch is cleanup for the case
        # where it is already dead; the desktop ping is deliberately left to
        # it, so a merge does not notify twice.
        stop_watcher(iid)
        return {"event": f"mr_{state}", "payload": payload}
    # Still open, or unreadable: the MR left *this filter*, which is not the
    # same claim as the MR ending. Its watcher keeps running — following an MR
    # the feed no longer returns is legitimate, and radar's prune makes the
    # same distinction.
    payload["mr_state"] = state or "unknown"
    return {
        "event": "mr_left_feed",
        "payload": payload,
        "notify_title": f"!{iid} left the feed",
        "notify_message": title,
    }


def poll(state: dict, ctx: dict) -> tuple[list[dict], dict]:
    scope = str(ctx.get("id") or defaults.DEFAULT_FEED_SCOPE)
    current = fetch_population(scope)
    if current is None:
        return [], state

    raw_known = state.get("known")
    baseline = not isinstance(raw_known, dict)
    known: dict[str, dict[str, str]] = raw_known if isinstance(raw_known, dict) else {}

    events: list[dict] = []
    watched = live_watchers()
    for iid, meta in current.items():
        if iid not in watched:
            spawn_watcher(iid)
        if baseline or iid in known:
            continue
        events.append({
            "event": "mr_opened",
            "payload": {"iid": iid, "url": meta["web_url"], "title": meta["title"]},
            "notify_title": f"!{iid} opened",
            "notify_message": meta["title"] or f"MR !{iid}",
        })

    for iid, meta in known.items():
        if iid not in current:
            events.append(_departure(iid, meta))

    return events, {"scope": scope, "known": current}


def is_terminal(state: dict) -> bool:
    """Never. A population has no final state, and a feed that stopped itself
    would restore the blindness this source exists to remove — silently, since
    a missing poller and a quiet one look the same. It ends on `unwatch`,
    SIGTERM, or the machine going away; `radar` respawns it after any of those.
    """
    return False
