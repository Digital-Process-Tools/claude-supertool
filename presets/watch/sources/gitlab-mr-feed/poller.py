


















































from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any




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



TERMINAL_EVENTS = ("merged", "closed")










EVENT_KEYS = (
    "mr_opened",
    "mr_merged",
    "mr_closed",
    "mr_left_feed",
    "mrs_unreachable",
)


LOOKUP_OK = "ok"
LOOKUP_UNAVAILABLE = "unavailable"

_dispatcher_module: ModuleType | None = None


def _dispatcher() -> ModuleType:


    global _dispatcher_module
    if _dispatcher_module is None:
        _dispatcher_module = _load("feed_watch_dispatcher", _WATCH_DIR / "dispatcher.py")
    return _dispatcher_module


def resolve_filter(scope: str) -> str:
    return ALIASES.get(scope, scope)


def fetch_population(scope: str) -> tuple[dict[str, dict[str, str]] | None, str]:



















    multi, _flags, unknown = mrs._parse_multi(resolve_filter(scope))
    if unknown:







        return None, (f"ERROR: scope {scope!r} carries a token gl-mrs cannot "
                      f"apply ({', '.join(sorted(unknown))}), so the "
                      f"population was not established")
    cfg = mrs._get_config()
    out: dict[str, dict[str, str]] = {}
    for filters in mrs._expand_filters(multi):
        try:
            result = mrs._run(mrs._build_list_cmd(filters, cfg["per_page"]))
        except FileNotFoundError:
            return None, "ERROR: glab not found — install the GitLab CLI"
        except subprocess.TimeoutExpired:



            return None, f"ERROR: glab timed out listing MRs for scope {scope!r}"
        except (OSError, ValueError, subprocess.SubprocessError) as err:




            return None, f"ERROR: glab could not run for scope {scope!r}: {err}"
        if result.returncode != 0:
            return None, mr_op._format_error(result.stderr or "", "MR list", scope)
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None, f"ERROR: invalid JSON from glab for scope {scope!r}"
        if not isinstance(data, list):
            return None, (f"ERROR: unexpected payload shape from glab for "
                          f"scope {scope!r}")
        for m in data:
            if not isinstance(m, dict) or m.get("iid") is None:
                continue
            out.setdefault(str(m["iid"]), {
                "title": str(m.get("title") or ""),
                "web_url": str(m.get("web_url") or ""),
            })
    return out, ""


def lookup_mr_state(iid: str) -> str:







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






    only = transport.read_state(defaults.DEFAULT_SOURCE, iid).get("only")
    if isinstance(only, list):
        return [str(e) for e in only]
    return None


def terminal_coverage(iid: str, watched: set[str], spawned: bool = False) -> list[str]:












    if spawned:


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

    title = meta.get("title") or f"MR !{iid}"
    url = meta.get("web_url") or ""
    payload = {"iid": iid, "url": url, "title": title}
    state = lookup_mr_state(iid)
    if state in TERMINAL_EVENTS:
        covers = meta.get("covers") or []
        if state in covers:






            return None




        stop_watcher(iid)
        return {"event": f"mr_{state}", "payload": payload}




    payload["mr_state"] = state or "unknown"
    return {
        "event": "mr_left_feed",
        "payload": payload,
        "notify_title": f"!{iid} left the feed",
        "notify_message": title,
    }


def poll(state: dict, ctx: dict) -> tuple[list[dict], dict]:
    scope = str(ctx.get("id") or defaults.DEFAULT_FEED_SCOPE)
    current, error = fetch_population(scope)

    raw_known = state.get("known")
    baseline = not isinstance(raw_known, dict)
    known: dict[str, dict[str, Any]] = raw_known if isinstance(raw_known, dict) else {}

    if current is None:












        new_state = {**state, "lookup": LOOKUP_UNAVAILABLE, "error": error}
        if state.get("lookup") == LOOKUP_UNAVAILABLE:
            return [], new_state
        return [{
            "event": "mrs_unreachable",
            "payload": {
                "scope": scope,
                "error": error,



                "last_known_count": len(known),
            },
            "notify_title": f"MR feed {scope} — cannot tell",
            "notify_message": error,
        }], new_state

    events: list[dict] = []
    watched = live_watchers()
    for iid, meta in current.items():
        spawned = False
        if iid not in watched:
            spawned = bool(spawn_watcher(iid))



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




    return events, {"scope": scope, "known": current, "lookup": LOOKUP_OK}


def is_terminal(state: dict) -> bool:





    return False
