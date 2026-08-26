"""github-pr-feed watcher source — continuous discovery of new PRs (#1780).

`github-pr` (this directory's sibling) polls one known PR number. Nothing in
that loop can discover a number that did not exist when the poller was
spawned, so a running watch session is structurally blind to a PR opened after
it started, and stays blind until a human re-runs `radar` or hand-spawns
`watch:github-pr:<n>`. Between two runs the board is not wrong-looking -- it is
confidently complete and missing a PR, which renders identically to all-green.
`gitlab-mr-feed` already closed this gap for GitLab; this is its GitHub twin,
built the same way for the reason #1780 gives: two working implementations of
one shape is evidence the shape is right, not license to invent a third one.

This source polls the *population* instead of one member of it:

    state       {number: {title, url}} for everything the filter returns
    new number  spawn watch:github-pr:<number>, emit pr_opened
    gone number look the PR up, then emit pr_merged / pr_closed / pr_left_feed
    no answer   emit prs_unreachable once, change nothing else
    terminal    never -- discovery has no end state

A population that could not be established is not an empty one. An
unreachable GitHub, an expired token or a scope carrying an unknown filter
token must never read as "every PR you had is gone", which would fire a
departure event for each of them (the #1602 shape, on this source's own
first poll).

The vocabulary is deliberately the tier's, not the op's (#1780 point 1 --
"which population"). `gh-prs` and `presets/watch/tiers/gh_prs.py` both narrow
`author`/`assignee`/`reviewer`/`label`/`state` out of the op's wider set for
their own reasons; a feed that discovered a PR the board it feeds would refuse
to show would be discovering something nobody asked for.

First poll records the baseline silently. Announcing every PR that was already
open when the feed started is not discovery, it is a notification storm.
Watchers are still spawned on that first poll, and on every poll after it, for
any number lacking a live poller -- coverage is continuous for the same reason
discovery is. Spawning is skipped outright under a repo target (#673): the
per-PR pid filename carries no repo, so a poller for #12 spawned here would be
indistinguishable from #12 of whatever clone started it, and radar's own
gh-prs tier already declines to heal for exactly this reason.

Two sources over one fact means one merge arrived twice -- `merged` from the
per-PR poller, `pr_merged` from here, seconds apart under different event keys
(the #434 shape). So `pr_merged`/`pr_closed` are emitted only when no per-PR
poller announces that transition itself. What "announces it itself" means is
recorded per number while the PR is still in the population, from the `only`
filter each poller publishes into its state file, because at departure time
the reporter is usually already gone: reporting the terminal state is what
ends it. Every unanswerable case resolves to *not covered*, so the feed
reports rather than stays silent -- the feed running with no per-PR pollers
behind it, or under a repo target where none can be spawned, is a supported
configuration, and turning this into a coverage hole would be a worse defect
than the duplicate it fixes.

Source plugin contract:
- INTERVAL: int seconds between polls
- poll(state, ctx) -> (events, new_state)
- is_terminal(state) -> bool
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

# PRs move on human timescales for the discovery question this source
# answers ("did one appear"), not on CI timescales -- the per-PR pollers this
# source spawns already carry the 30s traffic. Same number as
# gitlab-mr-feed's INTERVAL, for the same reasoning.
INTERVAL = 300

DEFAULT_SCOPE = "@open"

ALIASES = {
    "@open": "",
}

_GITHUB_DIR = Path(__file__).parents[3] / "github"
_WATCH_DIR = Path(__file__).parents[2]
_PRESETS_DIR = Path(__file__).parents[3]


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prs = _load("feed_github_prs", _GITHUB_DIR / "prs.py")
_gh = _load("feed_github_pr", _GITHUB_DIR / "pr.py")._gh
_format_error = _load("feed_github_run", _GITHUB_DIR / "run.py")._format_error
_repo_target = _load("feed_github_repo_target", _PRESETS_DIR / "_repo_target.py")
_filter_tokens = _load("feed_filter_tokens", _PRESETS_DIR / "_filter_tokens.py")
transport = _load("feed_watch_transport", _WATCH_DIR / "transport.py")

# The two transitions the per-PR source announces under its own event keys.
# The strings double as PR states, which is why one tuple serves both readings.
TERMINAL_EVENTS = ("merged", "closed")

# The filter keys this source honours -- the tier's own subset (#1780 point
# 1), not `gh-prs`'s wider one. `per`, `nopipe`, `iids`, `failed` and
# `anyauthor` are board shapes and enrichment knobs the op offers that a
# discovery feed has no business narrowing itself by.
KNOWN_FILTERS = {"author", "assignee", "reviewer", "label", "state"}
KNOWN_FLAGS: set[str] = set()
VALUE_DOMAINS: dict[str, object] = {"state": prs._STATES}

# One page. A population past this cannot be established in bounded time, and
# this source says so rather than reporting a prefix of it -- the same trap
# github-issue-feed's own MAX_PAGES exists to avoid, one call instead of a loop
# because `gh pr list --limit` does its own paging internally.
PER_PAGE = 200

# What this source can put on the wire. `events.json` is asserted equal to it,
# because a declared key nothing emits is an untrue claim and an emitted key
# nothing declares cannot be named in `only=`.
EVENT_KEYS = (
    "pr_opened",
    "pr_merged",
    "pr_closed",
    "pr_left_feed",
    "prs_unreachable",
)

LOOKUP_OK = "ok"
LOOKUP_UNAVAILABLE = "unavailable"

_dispatcher_module: ModuleType | None = None


def _dispatcher() -> ModuleType:
    """Lazy -- the dispatcher imports this module, so binding at import time
    would make each source load a second copy of its own loader."""
    global _dispatcher_module
    if _dispatcher_module is None:
        _dispatcher_module = _load("feed_watch_dispatcher", _WATCH_DIR / "dispatcher.py")
    return _dispatcher_module


def resolve_filters(scope: str) -> dict[str, str] | None:
    """`gh-prs`-vocabulary filters for a scope string, or `None` on an unknown
    token -- see the module docstring for why this vocabulary is narrower
    than the op's own.

    `None` is not an empty dict. An empty dict is a legitimate scope (every
    open PR on the repo, `gh-prs`'s own default since #1207); `None` is "this
    scope was not understood", and building the query anyway would widen the
    population past what was asked for.
    """
    resolved = ALIASES.get(scope, scope)
    filters, _flags, unknown = _filter_tokens.parse(resolved, KNOWN_FILTERS, KNOWN_FLAGS)
    if unknown:
        return None
    bad = _filter_tokens.bad_values(filters, VALUE_DOMAINS)
    if bad:
        return None
    return filters


def _unknown_reason(scope: str) -> str:
    """Recomputed rather than threaded alongside `None`, so the failure stays
    one value with one meaning -- naming the token is the difference between
    a refusal the operator can act on and one that sends them to the source."""
    resolved = ALIASES.get(scope, scope)
    filters, _flags, unknown = _filter_tokens.parse(resolved, KNOWN_FILTERS, KNOWN_FLAGS)
    if unknown:
        named = ", ".join(t.partition("=")[0] + "=" if "=" in t else t
                          for t in unknown)
        return (f"scope {scope!r} carries a token this source cannot apply "
                f"({named}). Known filters: {', '.join(sorted(KNOWN_FILTERS))}")
    bad = _filter_tokens.bad_values(filters, VALUE_DOMAINS)
    return f"scope {scope!r} " + _filter_tokens.value_error(bad)


def fetch_population(scope: str) -> tuple[dict[str, dict[str, str]] | None, str]:
    """`({number: {title, url}}, "")`, or `(None, why)` on any failure.

    `None` is deliberately not an empty dict: an unreachable GitHub must never
    read as "every PR you had is gone", which would fire a departure event for
    each of them.

    No check-rollup or review enrichment -- the feed answers "which PRs
    exist", and the per-PR watcher it spawns answers "what is happening to
    this one". One `gh pr list` call per poll, whatever the population size,
    capped at PER_PAGE.
    """
    filters = resolve_filters(scope)
    if filters is None:
        return None, f"ERROR: {_unknown_reason(scope)}"
    cmd = prs._build_list_cmd(filters, PER_PAGE, fields="number,title,url")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                                encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None, "ERROR: gh not found -- install from https://cli.github.com"
    except subprocess.TimeoutExpired:
        return None, f"ERROR: gh timed out listing PRs for scope {scope!r}"
    except (OSError, subprocess.SubprocessError) as err:
        return None, f"ERROR: gh could not run for scope {scope!r}: {err}"
    if result.returncode != 0:
        return None, _format_error(result.stderr or "", "Pull request list", scope)
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, f"ERROR: invalid JSON from gh for scope {scope!r}"
    if not isinstance(data, list):
        return None, f"ERROR: unexpected payload shape from gh for scope {scope!r}"
    if len(data) >= PER_PAGE:
        return None, (f"ERROR: scope {scope!r} returned {PER_PAGE} or more open "
                      f"PRs, so the population was not established -- narrow it "
                      f"with an author or label filter")
    out: dict[str, dict[str, str]] = {}
    for item in data:
        if not isinstance(item, dict) or item.get("number") is None:
            continue
        out[str(item["number"])] = {
            "title": str(item.get("title") or ""),
            "url": str(item.get("url") or ""),
        }
    return out, ""


def lookup_pr_state(number: str) -> str:
    """Live `state` of one PR (`OPEN`/`MERGED`/`CLOSED`), or "" when it could
    not be read.

    A number vanishing from the open population means merged, closed, or a
    changed filter. Guessing "merged" is right most of the time and
    confidently wrong the rest, so one extra call buys the truth. Departures
    are rare, so the call is too.
    """
    try:
        r = _gh(["pr", "view", number, "--json", "state"])
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return ""
    if r.returncode != 0:
        return ""
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("state") or "").upper()


def live_watchers() -> set[str] | None:
    return prs._watched_numbers(transport.STATE_DIR)


def watcher_only(number: str) -> list[str] | None:
    """The event filter of the per-PR poller for number -- None when
    unrecorded.

    None is deliberately not `[]`: an empty filter means "emits everything",
    while None means nothing could tell us what this poller will emit. Only
    one of those two answers is ever allowed to buy the feed's silence.
    """
    only = transport.read_state("github-pr", number).get("only")
    if isinstance(only, list):
        return [str(e) for e in only]
    return None


def terminal_coverage(number: str, watched: set[str], spawned: bool = False) -> list[str]:
    """Which of merged/closed a per-PR poller for number announces by itself.

    Liveness at the moment of the departure cannot answer this in either
    direction. Reporting `merged` is exactly what makes a per-PR poller
    terminal and end it, so by the time a number leaves the population its
    reporter is already gone -- and one that IS still alive is precisely the
    one that has not spoken yet, whose filter may exclude the event outright.
    So coverage is recorded while the PR is still in the population, from the
    filter the poller publishes into its state file, and every unanswerable
    case resolves to no coverage: the fallback is then a duplicate, which is
    visible and cheap, rather than an ending nobody reports at all.
    """
    if spawned:
        # Spawned here with an unfiltered poller (see spawn_watcher), so the
        # filter is known without waiting a tick for the new poller to
        # publish it.
        only: list[str] | None = []
    elif number in watched:
        only = watcher_only(number)
    else:
        only = None
    if only is None:
        return []
    if not only:
        return list(TERMINAL_EVENTS)
    return [e for e in TERMINAL_EVENTS if e in only]


def spawn_watcher(number: str) -> bool:
    try:
        return bool(_dispatcher()._spawn_poller("github-pr", number, []))
    except OSError:
        return False


def stop_watcher(number: str) -> None:
    try:
        _dispatcher().cmd_unwatch(["github-pr", number])
    except OSError:
        return


def _departure(number: str, meta: dict[str, Any]) -> dict[str, Any] | None:
    """The event for a number that left the population -- None when suppressed."""
    title = meta.get("title") or f"PR #{number}"
    url = meta.get("url") or ""
    payload = {"number": number, "url": url, "title": title}
    state = lookup_pr_state(number)
    lowered = state.lower()
    if lowered in TERMINAL_EVENTS:
        covers = meta.get("covers") or []
        if lowered in covers:
            # The per-PR watcher announces this transition under its own
            # event key, so the feed saying it again is one fact rendered as
            # two lines. It is deliberately not stopped here either: it has
            # either already ended (reporting the terminal state is what ends
            # it) or is alive and still owes us the event, and killing it in
            # that second case turns the suppressed duplicate into silence.
            return None
        # Nobody else is going to report this one, so the feed does -- and
        # the unwatch stays on this path, where it is the stale-PID cleanup
        # it was always meant to be. The desktop ping is still left to the
        # per-PR tier: a merge should ping once or not at all, never twice.
        stop_watcher(number)
        return {"event": f"pr_{lowered}", "payload": payload}
    # Still open, or unreadable: the PR left *this filter*, which is not the
    # same claim as the PR ending. Its watcher keeps running -- following a PR
    # the feed no longer returns is legitimate.
    payload["pr_state"] = state or "unknown"
    return {
        "event": "pr_left_feed",
        "payload": payload,
        "notify_title": f"#{number} left the feed",
        "notify_message": title,
    }


def poll(state: dict, ctx: dict) -> tuple[list[dict], dict]:
    scope = str(ctx.get("id") or DEFAULT_SCOPE)
    current, error = fetch_population(scope)

    raw_known = state.get("known")
    baseline = not isinstance(raw_known, dict)
    known: dict[str, dict[str, Any]] = raw_known if isinstance(raw_known, dict) else {}

    if current is None:
        # Three answers, not two: ok, a finding, and *cannot tell*.
        #
        # Said once per outage rather than once per poll. An alert repeating
        # every five minutes for an expired token is one people mute, and a
        # muted alert is the original silence by a longer route.
        #
        # `{**state, ...}` is the recovery guarantee, and the part a naive
        # port breaks. `known` carries both the population and each member's
        # `covers`; reset it and the first successful poll after the outage
        # re-announces every open PR as a `pr_opened` -- one notification
        # storm per network blip, which is the failure the baseline rule at
        # the top of this file exists to prevent.
        new_state = {**state, "lookup": LOOKUP_UNAVAILABLE, "error": error}
        if state.get("lookup") == LOOKUP_UNAVAILABLE:
            return [], new_state
        return [{
            "event": "prs_unreachable",
            "payload": {
                "scope": scope,
                "error": error,
                # `last_known_`, not a bare count: this tick read nothing, so
                # the number describes the last poll that could see rather
                # than what GitHub holds now.
                "last_known_count": len(known),
            },
            "notify_title": f"PR feed {scope} -- cannot tell",
            "notify_message": error,
        }], new_state

    events: list[dict] = []
    watched = live_watchers()
    # `None` under a repo target (#673): a poller spawned here for #N would be
    # indistinguishable from #N of whatever clone started it. `set()`, not
    # `None`, past this point -- every `number not in watched` check below
    # then treats every number as unwatched, and `spawn_watcher` is simply
    # never called because coverage_known is False.
    coverage_known = watched is not None
    watched_set: set[str] = watched if watched is not None else set()
    for number, meta in current.items():
        spawned = False
        if coverage_known and number not in watched_set:
            spawned = bool(spawn_watcher(number))
        # Recorded now, while the PR is still here to be observed. At
        # departure the per-PR poller is typically already gone, and a state
        # file that no longer exists cannot be told apart from one that never
        # did.
        meta["covers"] = terminal_coverage(number, watched_set, spawned=spawned)
        if baseline or number in known:
            continue
        events.append({
            "event": "pr_opened",
            "payload": {"number": number, "url": meta["url"], "title": meta["title"]},
            "notify_title": f"#{number} opened",
            "notify_message": meta["title"] or f"PR #{number}",
        })

    for number, meta in known.items():
        if number not in current:
            departure = _departure(number, meta)
            if departure is not None:
                events.append(departure)

    # `lookup` is rewritten on every successful poll, not only when it was
    # unavailable: a flag that clears lazily makes the *next* outage silent,
    # which is this defect again one tick later.
    return events, {"scope": scope, "known": current, "lookup": LOOKUP_OK}


def is_terminal(state: dict) -> bool:
    """Never. A population has no final state, and a feed that stopped itself
    would restore the blindness this source exists to remove -- silently,
    since a missing poller and a quiet one look the same. It ends on
    `unwatch`, SIGTERM, or the machine going away; `radar` respawns it after
    any of those.
    """
    return False
