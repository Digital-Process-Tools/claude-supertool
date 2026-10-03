


























































from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any





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



ratelimit = _load("feed_watch_ratelimit", _WATCH_DIR / "ratelimit.py")



TERMINAL_EVENTS = ("merged", "closed")





KNOWN_FILTERS = {"author", "assignee", "reviewer", "label", "state"}
KNOWN_FLAGS: set[str] = set()
VALUE_DOMAINS: dict[str, object] = {"state": prs._STATES}





PER_PAGE = 200




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


    global _dispatcher_module
    if _dispatcher_module is None:
        _dispatcher_module = _load("feed_watch_dispatcher", _WATCH_DIR / "dispatcher.py")
    return _dispatcher_module


_radar_module: ModuleType | None = None
_gh_prs_tier_module: ModuleType | None = None
_pr_only_cache: list[str] | None = None


def _radar() -> ModuleType:



    global _radar_module
    if _radar_module is None:
        _radar_module = _load("feed_watch_radar", _WATCH_DIR / "radar.py")
    return _radar_module


def _gh_prs_tier() -> ModuleType:






    global _gh_prs_tier_module
    if _gh_prs_tier_module is None:
        _gh_prs_tier_module = _load("feed_watch_gh_prs_tier",
                                    _WATCH_DIR / "tiers" / "gh_prs.py")
    return _gh_prs_tier_module


def pr_only() -> list[str]:



























    global _pr_only_cache
    if _pr_only_cache is not None:
        return _pr_only_cache
    try:
        tiers, _problems = _radar().read_tiers()
        tier = _gh_prs_tier()
        opts = tiers.get("gh-prs") or {}
        excluded = tier.exclude_events(opts.get(tier.PR_EXCLUDE_OPTION))
        _pr_only_cache = tier.poller_only(excluded)
    except Exception:
        _pr_only_cache = []
    return _pr_only_cache


def resolve_filters(scope: str) -> dict[str, str] | None:









    resolved = ALIASES.get(scope, scope)
    filters, _flags, unknown = _filter_tokens.parse(resolved, KNOWN_FILTERS, KNOWN_FLAGS)
    if unknown:
        return None
    bad = _filter_tokens.bad_values(filters, VALUE_DOMAINS)
    if bad:
        return None
    return filters


def _unknown_reason(scope: str) -> str:



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











    filters = resolve_filters(scope)
    if filters is None:
        return None, f"ERROR: {_unknown_reason(scope)}"
    cmd = prs._build_list_cmd(filters, PER_PAGE, fields="number,title,url")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                                encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None, "ERROR: gh not found -- install the GitHub CLI"
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







    only = transport.read_state("github-pr", number).get("only")
    if isinstance(only, list):
        return [str(e) for e in only]
    return None


def terminal_coverage(number: str, watched: set[str], spawned: bool = False) -> list[str]:












    if spawned:



        only: list[str] | None = pr_only()
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
        return bool(_dispatcher()._spawn_poller("github-pr", number, pr_only()))
    except OSError:
        return False


def stop_watcher(number: str) -> None:
    try:
        _dispatcher().cmd_unwatch(["github-pr", number])
    except OSError:
        return


def _departure(number: str, meta: dict[str, Any]) -> dict[str, Any] | None:

    title = meta.get("title") or f"PR #{number}"
    url = meta.get("url") or ""
    payload = {"number": number, "url": url, "title": title}
    state = lookup_pr_state(number)
    lowered = state.lower()
    if lowered in TERMINAL_EVENTS:
        covers = meta.get("covers") or []
        if lowered in covers:






            return None




        stop_watcher(number)
        return {"event": f"pr_{lowered}", "payload": payload}



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















        extra = ratelimit.unreachable_extra(error)
        new_state = {**state, "lookup": LOOKUP_UNAVAILABLE, "error": error, **extra}
        if state.get("lookup") == LOOKUP_UNAVAILABLE:
            return [], new_state
        payload = {
            "scope": scope,
            "error": error,



            "last_known_count": len(known),
        }
        if "retry_after" in extra:
            payload["retry_after"] = extra["retry_after"]
        return [{
            "event": "prs_unreachable",
            "payload": payload,
            "notify_title": f"PR feed {scope} -- cannot tell",
            "notify_message": error,
        }], new_state

    events: list[dict] = []
    watched = live_watchers()





    coverage_known = watched is not None
    watched_set: set[str] = watched if watched is not None else set()
    for number, meta in current.items():
        spawned = False
        if coverage_known and number not in watched_set:
            spawned = bool(spawn_watcher(number))




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




    return events, {"scope": scope, "known": current, "lookup": LOOKUP_OK}


def is_terminal(state: dict) -> bool:






    return False
