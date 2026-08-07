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

Two tiers over one fact means one merge arrived twice — `merged` from the
per-MR poller, `mr_merged` from here, seconds apart under different event keys
(#434). So `mr_merged`/`mr_closed` are emitted only when no per-MR poller
announces that transition itself. What "announces it itself" means is recorded
per iid while the MR is still in the population, from the `only` filter each
poller publishes into its state file, because at departure time the reporter
is usually already gone: reporting the terminal state is what ends it. Every
unanswerable case resolves to *not covered*, so the feed reports rather than
stays silent — the feed running with no per-MR pollers behind it is a
supported configuration, and turning this into a coverage hole would be a
worse defect than the duplicate it fixes.

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
transport = _load("feed_watch_transport", _WATCH_DIR / "transport.py")

# The two transitions the per-MR source announces under its own event keys.
# The strings double as MR states, which is why one tuple serves both readings.
TERMINAL_EVENTS = ("merged", "closed")

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
    `glab mr list` call per poll and per author, whatever the population size:
    a repeated key in the scope fans out, because GitLab takes one
    `author_username` per query and the scope describes their union. Any one
    of those calls failing fails the whole poll — a partial population reads
    as a departure for everything the missing query would have returned.
    """
    multi, _flags, unknown = mrs._parse_multi(resolve_filter(scope))
    if unknown:
        # A scope carrying a token gl-mrs cannot apply describes a *wider*
        # population than the caller asked for, so building it anyway would
        # spawn watchers over strangers' MRs and fire an mr_opened for each.
        # None is this function's "could not establish the population", and it
        # is the safe direction: no events either way (#939).
        return None
    cfg = mrs._get_config()
    out: dict[str, dict[str, str]] = {}
    for filters in mrs._expand_filters(multi):
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
        for m in data:
            if not isinstance(m, dict) or m.get("iid") is None:
                continue
            out.setdefault(str(m["iid"]), {
                "title": str(m.get("title") or ""),
                "web_url": str(m.get("web_url") or ""),
            })
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


def watcher_only(iid: str) -> list[str] | None:
    """The event filter of the per-MR poller for iid — None when unrecorded.

    None is deliberately not `[]`: an empty filter means "emits everything",
    while None means nothing could tell us what this poller will emit. Only one
    of those two answers is ever allowed to buy the feed's silence.
    """
    only = transport.read_state(defaults.DEFAULT_SOURCE, iid).get("only")
    if isinstance(only, list):
        return [str(e) for e in only]
    return None


def terminal_coverage(iid: str, watched: set[str], spawned: bool = False) -> list[str]:
    """Which of merged/closed a per-MR poller for iid announces by itself.

    Liveness at the moment of the departure cannot answer this in either
    direction. Reporting `merged` is exactly what makes a per-MR poller
    terminal and end it, so by the time an iid leaves the population its
    reporter is already gone — and one that IS still alive is precisely the one
    that has not spoken yet, whose filter may exclude the event outright. So
    coverage is recorded while the MR is still in the population, from the
    filter the poller publishes into its state file, and every unanswerable
    case resolves to no coverage: the fallback is then a duplicate, which is
    visible and cheap, rather than an ending nobody reports at all.
    """
    if spawned:
        # Spawned here, so the filter is known without waiting a tick for the
        # new poller to publish it.
        only: list[str] | None = [e for e in defaults.DEFAULT_ONLY.split(",") if e]
    elif iid in watched:
        only = watcher_only(iid)
    else:
        only = None
    if only is None:
        return []
    if not only:
        return list(TERMINAL_EVENTS)
    return [e for e in TERMINAL_EVENTS if e in only]


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


def _departure(iid: str, meta: dict[str, Any]) -> dict[str, Any] | None:
    """The event for an iid that left the population — None when suppressed."""
    title = meta.get("title") or f"MR !{iid}"
    url = meta.get("web_url") or ""
    payload = {"iid": iid, "url": url, "title": title}
    state = lookup_mr_state(iid)
    if state in TERMINAL_EVENTS:
        covers = meta.get("covers") or []
        if state in covers:
            # The per-MR watcher announces this transition under its own event
            # key, so the feed saying it again is one fact rendered as two
            # lines. It is deliberately not stopped here either: it has either
            # already ended (reporting the terminal state is what ends it) or
            # is alive and still owes us the event, and killing it in that
            # second case turns the suppressed duplicate into silence.
            return None
        # Nobody else is going to report this one, so the feed does — and the
        # unwatch stays on this path, where it is the stale-PID cleanup it was
        # always meant to be. The desktop ping is still left to the per-MR
        # tier: a merge should ping once or not at all, never twice.
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
    known: dict[str, dict[str, Any]] = raw_known if isinstance(raw_known, dict) else {}

    events: list[dict] = []
    watched = live_watchers()
    for iid, meta in current.items():
        spawned = False
        if iid not in watched:
            spawned = bool(spawn_watcher(iid))
        # Recorded now, while the MR is still here to be observed. At departure
        # the per-MR poller is typically already gone, and a state file that no
        # longer exists cannot be told apart from one that never did.
        meta["covers"] = terminal_coverage(iid, watched, spawned=spawned)
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
            departure = _departure(iid, meta)
            if departure is not None:
                events.append(departure)

    return events, {"scope": scope, "known": current}


def is_terminal(state: dict) -> bool:
    """Never. A population has no final state, and a feed that stopped itself
    would restore the blindness this source exists to remove — silently, since
    a missing poller and a quiet one look the same. It ends on `unwatch`,
    SIGTERM, or the machine going away; `radar` respawns it after any of those.
    """
    return False
