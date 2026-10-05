#!/usr/bin/env python3




































































































































from __future__ import annotations

import datetime
import glob
import hashlib
import importlib.util
import json
import os
import sys
from typing import Any

from pathlib import Path

_HERE = Path(__file__).parent
_WATCH = _HERE.parent

sys.path.insert(0, str(_WATCH))
import defaults  
import dispatcher  
import transport  

sys.path.insert(0, str(_WATCH.parent))
import _filter_tokens  


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mrs = _load("radar_gitlab_mrs", _WATCH.parent / "gitlab" / "mrs.py")



snapshot = _load("radar_snapshot", _HERE / "_snapshot.py")



_auth_probe = _load("radar_auth_probe", _WATCH.parent / "_auth_probe.py")











sys.path.insert(0, str(_HERE))
import _radar_errors  






RadarError = _radar_errors.RadarError
RadarUnreachable = _radar_errors.RadarUnreachable









RadarUnconfigured = _radar_errors.RadarUnconfigured









NOT_AUTHENTICATED_MARKERS = (_auth_probe.NOT_AUTHENTICATED_MARKERS
                             + _auth_probe.GITLAB_MARKERS)
































TRANSPORT_MARKERS = (
    "dial tcp",
    "no such host",
    "connection refused",
    "connection reset",
    "network is unreachable",
    "i/o timeout",
    "tls handshake timeout",
    "client.timeout",
    "error connecting to",



    "429 too many requests",
    "rate limit",
)


def _transport_unreachable(err: str) -> bool:

    low = err.lower()
    return any(marker in low for marker in TRANSPORT_MARKERS)

SOURCE = defaults.DEFAULT_SOURCE
FEED_SOURCE = defaults.DEFAULT_FEED_SOURCE
FEED_SCOPE = defaults.DEFAULT_FEED_SCOPE
SNAPSHOT_PREFIX = "supertool-radar"



EXCLUSIONS_ENV = "SUPERTOOL_RADAR_EXCLUSIONS"

_FIX_HINT = "remove it from ops.radar.radar_exclusions in .supertool.json"

FEED_LABEL = {"alive": "feed ok", "spawned": "feed respawned", "failed": "feed DOWN",
              "capped": "feed DOWN (respawn capped)"}




RADAR_OPTIONS = {"quiet_when_healthy", "stale_running_minutes"}




STALE_RUNNING_MINUTES = 240





IN_PROGRESS_PIPELINES = frozenset({"running", "pending"})




RADAR_QUIET_DEFAULT = False












KNOWN_FILTERS = set(mrs._FILTER_FLAG) | {"state"}














KNOWN_FLAGS: set[str] = set()






VALUE_DOMAINS: dict[str, object] = {"state": mrs._STATES}


def _parse(arg: str) -> dict[str, list[str]]:

    multi, _flags, unknown = _filter_tokens.parse_multi(
        arg, KNOWN_FILTERS, KNOWN_FLAGS)
    if unknown:
        raise RadarError(
            "radar: gl-mrs tier " + _filter_tokens.unknown_error(
                unknown, KNOWN_FILTERS, KNOWN_FLAGS)
            + " Radar does not just print this population, it watches it: an "
              "unapplied token widens the scope, and the fleet then spawns over "
              "MRs nobody asked about."
        )
    bad = [b for key, vals in multi.items() for v in vals
           for b in _filter_tokens.bad_values({key: v}, VALUE_DOMAINS)]
    if bad:
        raise RadarError("radar: gl-mrs tier " + _filter_tokens.value_error(bad))
    return multi


def default_filter() -> dict[str, list[str]]:







    return _parse(defaults.DEFAULT_FILTER)


def resolve_filter(arg: str = "") -> dict[str, list[str]]:



































    arg = (arg or "").strip()
    multi = _parse(arg) if arg else {}
    return multi or default_filter()


def filter_string(multi: dict[str, list[str]]) -> str:

    return ",".join(f"{k}={v}" for k, vals in multi.items() for v in vals)


def canonical_filter_string(multi: dict[str, list[str]]) -> str:












    return ",".join(f"{k}={v}"
                    for k, vals in sorted(multi.items())
                    for v in sorted(set(vals)))


def filter_key(multi: dict[str, list[str]]) -> str:









    return snapshot.key({k: sorted(set(v)) for k, v in sorted(multi.items())})


def _snapshot_path(multi: dict[str, list[str]]) -> str:
    return snapshot.path(SNAPSHOT_PREFIX, filter_key(multi))






def _query(filters: dict[str, str], per_page: int) -> list[dict]:














    try:
        result = mrs._run(mrs._build_list_cmd(filters, per_page))
    except Exception as exc:  



        raise RadarUnreachable(f"glab mr list failed: {exc}") from exc
    if result.returncode < 0:




























        err = mrs._untrusted.flat((result.stderr or "").strip()) or "unknown error"
        raise RadarUnreachable(
            f"glab mr list did not finish before it answered (returncode "
            f"{result.returncode}, consistent with a killing signal on "
            f"POSIX — not established on Windows, see #1871): {err}")
    if result.returncode != 0:




        err = mrs._untrusted.flat((result.stderr or "").strip()) or "unknown error"
        if _auth_probe.says_not_authenticated(err, _auth_probe.GITLAB_MARKERS):








            raise RadarUnreachable(
                f"glab says this request was not authenticated (exit "
                f"{result.returncode}): {err}. Run: glab auth login")
        if _transport_unreachable(err):







            raise RadarUnreachable(
                f"glab could not reach the API (exit {result.returncode}): "
                f"{err}")




        raise RadarError(
            f"glab mr list did not answer, and nothing in its output says why "
            f"(exit {result.returncode}): {err}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RadarError("could not parse glab JSON output") from exc
    if not isinstance(data, list):
        raise RadarError("glab returned no MR list")
    return data


def live_open_mrs(multi: dict[str, list[str]] | None = None) -> list[dict]:






    multi = default_filter() if multi is None else multi
    cfg = mrs._get_config()
    merged: dict[str, dict] = {}
    for filters in mrs._expand_filters(multi):
        for m in _query(filters, cfg["per_page"]):
            if isinstance(m, dict) and m.get("iid") is not None:
                merged.setdefault(str(m["iid"]), m)
    data = list(merged.values())
    mrs._enrich(data, cfg["enrich_cap"], cfg["enrich_workers"])
    return data






def read_state_files() -> dict[str, dict]:





















    prefix = f"supertool-watch-{SOURCE}__"
    suffix = ".state.json"
    out: dict[str, dict] = {}
    pattern = os.path.join(transport.STATE_DIR, f"{prefix}*{suffix}")
    for path in sorted(glob.glob(pattern)):
        iid = os.path.basename(path)[len(prefix):-len(suffix)]
        if not iid:
            continue
        loaded, _refusal = transport.read_state_checked(SOURCE, iid)
        if loaded:
            out[iid] = loaded
    return out


def prune_terminal(states: dict[str, dict], watched: set[str]) -> list[str]:








    poller = dispatcher._load_source(SOURCE)
    is_terminal = getattr(poller, "is_terminal", None) if poller else None
    if is_terminal is None:
        return []
    pruned: list[str] = []
    for iid, full in states.items():
        if iid in watched:
            continue
        if is_terminal(full.get("source_state") or {}):
            if transport.clear_state(SOURCE, iid):
                pruned.append(iid)
    return pruned


def drift(states: dict[str, dict]) -> dict[str, tuple[str, str]]:





    out: dict[str, tuple[str, str]] = {}
    for iid, full in states.items():
        event_pipe = str(((full.get("last_event") or {}).get("payload") or {}).get("pipeline_id") or "")
        live_pipe = str((full.get("source_state") or {}).get("pipeline_id") or "")
        if event_pipe and live_pipe and event_pipe != live_pipe:
            out[iid] = (event_pipe, live_pipe)
    return out






def heal(open_iids: list[str], watched: set[str]) -> tuple[list[str], list[str], list[str]]:




























    gaps = [iid for iid in open_iids if iid not in watched]
    if not gaps:
        return [], [], []
    if dispatcher._load_source(SOURCE) is None:
        return [], gaps, []
    only = [e for e in defaults.DEFAULT_ONLY.split(",") if e]
    healed: list[str] = []
    failed: list[str] = []
    refused: list[str] = []
    for iid in gaps:
        transport.reap_dead_pidfile(SOURCE, iid)
        if len(transport.deaths(SOURCE, iid)) >= transport.DEATH_RESPAWN_LIMIT:
            refused.append(iid)
            continue
        status, _pid = dispatcher.start_poller(SOURCE, iid, only)
        if status == "spawned":
            healed.append(iid)
        elif status in ("failed", "unclaimable"):



            failed.append(iid)
    return healed, failed + refused, refused


def loss_warnings(healed: list[str], refused: list[str]) -> list[str]:








    out: list[str] = []
    for iid in refused:
        n = len(transport.deaths(SOURCE, iid))
        out.append(f"radar: WARNING — !{iid} has lost its poller {n} times; "
                   f"NOT respawning. This MR is unwatched until the cause is "
                   f"fixed and it is re-armed: "
                   f"./supertool 'watch:{SOURCE}:{iid}'.")
    for iid in healed:
        recorded = transport.deaths(SOURCE, iid)
        if not recorded:
            continue
        last = recorded[-1].get("pid", "?")
        out.append(f"radar: NOTE — !{iid} lost its poller (PID {last} died "
                   f"without being unwatched, {len(recorded)} recorded); "
                   f"respawned.")
    return out






def feed_scope(multi: dict[str, list[str]] | None = None) -> str:










    multi = default_filter() if multi is None else multi
    poller = dispatcher._load_source(FEED_SOURCE)
    aliases = getattr(poller, "ALIASES", None) or {}
    for alias, expansion in aliases.items():
        if mrs._parse_multi(expansion)[0] == multi:
            return alias
    return canonical_filter_string(multi)


def feed_pid(scope: str = FEED_SCOPE) -> int:

    return transport.read_pid(FEED_SOURCE, scope)


def feed_only() -> list[str]:

    return [e for e in defaults.DEFAULT_FEED_ONLY.split(",") if e]


def other_feed_scopes(scope: str = FEED_SCOPE) -> list[str]:













    return sorted({
        str(row.get("id") or "")
        for row in transport.list_active_pids()
        if row.get("source") == FEED_SOURCE and str(row.get("id") or "") != scope
    } - {""})


def feed_error(scope: str = FEED_SCOPE) -> str:







    state = transport.read_state(FEED_SOURCE, scope)







    return mrs._untrusted.flat(str((state.get("last_error") or {}).get("message") or ""))






FEED_LOOKUP_UNAVAILABLE = "unavailable"


def feed_blind(scope: str = FEED_SCOPE) -> str:













    state = transport.read_state(FEED_SOURCE, scope).get("source_state") or {}
    if not isinstance(state, dict) or state.get("lookup") != FEED_LOOKUP_UNAVAILABLE:
        return ""
    return mrs._untrusted.flat(str(state.get("error") or ""))






def _snap_entry(m: dict) -> dict[str, Any]:

    return {
        "pipeline": str(m.get("_pipeline") or ""),
        "pipeline_id": str(m.get("_pipeline_id") or ""),
        "draft": bool(m.get("draft")),



        "conflict": mrs._conflict_label(m),
    }


def read_snapshot(multi: dict[str, list[str]] | None = None) -> dict[str, Any] | None:





    multi = default_filter() if multi is None else multi
    return snapshot.read(SNAPSHOT_PREFIX, filter_key(multi), "mrs")


def write_snapshot(entries: dict[str, dict],
                   multi: dict[str, list[str]] | None = None) -> None:
    snapshot.write(SNAPSHOT_PREFIX,
                   filter_key(default_filter() if multi is None else multi),
                   entries, "mrs")


def _departed(previous: dict[str, Any] | None,
              open_mrs: list[dict]) -> list[str]:






    prev_entries: dict[str, Any] = (previous or {}).get("mrs", {}) or {}
    live = {str(m.get("iid")) for m in open_mrs}
    return [i for i in prev_entries if i not in live]


def _marks(iid: str, drifted: dict[str, tuple[str, str]],
           healed: set[str], uncovered: set[str],
           stale_minutes: float = 0.0, stale_state: str = "") -> str:

    out = []
    if iid in drifted:
        was, now = drifted[iid]





        out.append(f"[drift: {mrs._untrusted.flat(was)}→{mrs._untrusted.flat(now)}]")
    if stale_minutes:
        out.append(f"[{snapshot.unchanged_label(stale_minutes, stale_state)}]")
    if iid in healed:
        out.append("[healed]")
    elif iid in uncovered:
        out.append("[unwatched]")
    return ("  " + " ".join(out)) if out else ""


def _is_standing_problem(m: dict) -> bool:

    return bool(_problem_label(m))


def _stale_running(m: dict, previous_entry: Any, threshold: float,
                   now: str | None = None) -> float:










    if threshold <= 0:
        return 0.0
    if str(m.get("_pipeline") or "") not in IN_PROGRESS_PIPELINES:
        return 0.0
    mins = snapshot.unchanged_minutes(previous_entry, now)
    if mins is None or mins < threshold:
        return 0.0
    return mins


def _problem_label(m: dict) -> str:









    bits = []
    if str(m.get("_pipeline") or "") == "failed":
        bits.append("failed")
    blocked = mrs._conflict_label(m)
    if blocked:
        bits.append(blocked)
    return "+".join(bits)






def _refused(iid: str, why: str) -> str:
    return (f"radar: exclusion !{iid} REFUSED — {why}. The row is shown; "
            f"fix ops.radar.radar_exclusions in .supertool.json")


def _not_applied(iid: str, why: str) -> str:
    return f"radar: exclusion !{iid} NOT applied — {why}; {_FIX_HINT}"


def read_exclusions(raw: str | None = None) -> tuple[dict[str, dict[str, str]], list[str]]:













    raw = os.environ.get("SUPERTOOL_RADAR_EXCLUSIONS", "") if raw is None else raw
    raw = raw.strip()
    if not raw:
        return {}, []
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, [f"radar: WARNING — radar_exclusions is not valid JSON ({exc.msg}). "
                    f"No exclusions applied."]
    if not isinstance(loaded, dict):
        return {}, ["radar: WARNING — radar_exclusions must be an object keyed by MR "
                    "iid. No exclusions applied."]

    out: dict[str, dict[str, str]] = {}
    problems: list[str] = []
    for key, spec in loaded.items():
        iid = str(key).strip().lstrip("!")
        if isinstance(spec, str):
            spec = {"reason": spec}
        if not isinstance(spec, dict):
            problems.append(_refused(iid, "it is neither a reason string nor an object"))
            continue
        if not iid.isdigit():
            problems.append(_refused(iid, "the key is not an MR iid"))
            continue
        reason = str(spec.get("reason") or "").strip()
        if not reason:
            problems.append(_refused(iid, "it carries no reason"))
            continue
        out[iid] = {"reason": reason, "until": str(spec.get("until") or "").strip()}
    return out, problems


def resolve_exclusions(open_mrs: list[dict], exclusions: dict[str, dict[str, str]],
                       covered: set[str], today: str = "") -> tuple[set[str], list[str]]:












    if not exclusions:
        return set(), []
    today = today or datetime.date.today().isoformat()
    by_iid = {str(m.get("iid")): m for m in open_mrs if m.get("iid") is not None}

    suppressed: set[str] = set()
    lines: list[str] = []
    for iid in sorted(exclusions):
        spec = exclusions[iid]
        m = by_iid.get(iid)
        if m is None:
            lines.append(_not_applied(iid, "no open MR with that iid in this population"))
            continue
        until = spec["until"]
        if until and until < today:
            lines.append(_not_applied(iid, f"expired {until}"))
            continue
        label = _problem_label(m)
        if not label:
            pipe = str(m.get("_pipeline") or "none")
            lines.append(_not_applied(
                iid, f"pipeline is '{pipe}' and there is no conflict, so the "
                     f"reason is spent"))
            continue
        suppressed.add(iid)
        cover = "still watched" if iid in covered else "UNWATCHED"
        lines.append(f"radar: excluded !{iid} {label}, {cover} — {spec['reason']}")
    return suppressed, lines


def _footer(open_mrs: list[dict], covered: set[str], healed: list[str],
            drifted: dict[str, tuple[str, str]], pruned: list[str],
            uncovered: list[str], gone: int, feed: str, label: str = "",
            excluded: int = 0, elided: int = 0,
            departed_capped: bool = False) -> str:
















    counts: dict[str, int] = {}
    for m in open_mrs:
        counts[str(m.get("_pipeline") or "none")] = counts.get(str(m.get("_pipeline") or "none"), 0) + 1
    parts = [label] if label else []
    parts.append(f"{len(open_mrs)} open")
    if counts.get("failed"):
        parts.append(f"{counts['failed']} failing")
    if counts.get("running"):
        parts.append(f"{counts['running']} running")
    if counts.get("success"):
        parts.append(f"{counts['success']} green")





    unchecked = mrs._unchecked_count(open_mrs)
    if unchecked:
        parts.append(f"{unchecked} unchecked")
    parts.append(f"{len([m for m in open_mrs if str(m.get('iid')) in covered])} watched")
    if healed:
        parts.append(f"{len(healed)} healed")
    if uncovered:
        parts.append(f"{len(uncovered)} unwatched")
    if drifted:
        parts.append(f"{len(drifted)} drift")
    if pruned:
        parts.append(f"{len(pruned)} pruned")
    if gone:


        parts.append(f"{gone} off this page" if departed_capped
                     else f"{gone} left this board")
    if excluded:
        parts.append(f"{excluded} excluded")
    if elided:
        parts.append(f"{elided} unchanged not shown")
    parts.append(FEED_LABEL.get(feed, feed))
    return " | ".join(parts)


def _feed_warnings(feed: str, feed_err: str,
                   others: list[str] | None = None,
                   blind: str = "") -> list[str]:









    out: list[str] = []
    if feed == "failed":
        out.append("radar: WARNING — MR feed poller is down. New MRs will not be "
                   "discovered until the next radar run.")
    elif feed == "capped":
        out.append("radar: WARNING — MR feed poller has died too often and is no "
                   "longer being respawned. New MRs will NOT be discovered.")
    elif feed_err:
        out.append(f"radar: WARNING — MR feed poller is failing to poll: {feed_err}")



    if blind:
        out.append(f"radar: WARNING — MR feed poller is alive but could not "
                   f"establish its population, so nothing has been discovered "
                   f"since: {blind}")
    for other in others or []:
        out.append(f"radar: NOTE — a feed poller is also live on scope '{other}', "
                   f"which this board does not cover. Its MRs are not on this "
                   f"board; re-run as radar:{other} to see them.")
    return out


def _unchecked_warning(unchecked: int, total: int) -> list[str]:

















    if unchecked <= 0:
        return []
    cap = mrs._get_config()["enrich_cap"]
    line = (f"radar: WARNING — {unchecked} of {total} MRs on this board were not "
            f"checked: their pipeline status is unknown, not green, so a failing "
            f"one among them is indistinguishable from a passing one here.")




    if total > cap:
        line += f" Enrichment cap is {cap}; raise {mrs.ENRICH_CAP_KNOB}=N."
    return [line]


def render(open_mrs: list[dict], covered: set[str], healed: list[str],
           drifted: dict[str, tuple[str, str]], pruned: list[str],
           uncovered: list[str], previous: dict[str, Any] | None,
           feed: str = "alive", feed_err: str = "", label: str = "",
           excluded: set[str] | None = None, notes: list[str] | None = None,
           other_scopes: list[str] | None = None,
           losses: list[str] | None = None,
           page_capped: bool = False,
           now: str | None = None,
           stale_running_minutes: float = STALE_RUNNING_MINUTES,
           feed_blind: str = "") -> list[str]:


















    cold = previous is None
    prev_entries: dict[str, Any] = (previous or {}).get("mrs", {}) or {}
    healed_set, uncovered_set = set(healed), set(uncovered)
    excluded = excluded or set()
    board_mrs = [m for m in open_mrs if str(m.get("iid", "?")) not in excluded]

    shown = []
    elided: list[str] = []
    for m in sorted(board_mrs, key=mrs._sort_key):
        iid = str(m.get("iid", "?"))
        prev_entry = prev_entries.get(iid)


        moved = snapshot.facts(prev_entry) != _snap_entry(m)
        notable = iid in drifted or iid in healed_set or iid in uncovered_set
        stale = _stale_running(m, prev_entry, stale_running_minutes, now)
        if cold or moved or notable or _is_standing_problem(m) or stale:
            marks = _marks(iid, drifted, healed_set, uncovered_set, stale,
                           str(m.get("_pipeline") or ""))
            shown.append(mrs._row(m, covered, True, marks))
        else:
            elided.append(iid)




    departed = _departed(previous, open_mrs)
    footer = _footer(board_mrs, covered, healed, drifted, pruned, uncovered,
                     len(departed), feed, label, len(excluded), len(elided),
                     page_capped)





    elision = (snapshot.elided_note(elided, len(board_mrs), "MRs", "!", "gl-mrs")
               if shown else [])

    lines = (_feed_warnings(feed, feed_err, other_scopes, feed_blind)
             + _unchecked_warning(mrs._unchecked_count(open_mrs), len(open_mrs))
             + elision
             + snapshot.departed_note(departed, "MR", "!", "gl-mr:<iid>",
                                      page_capped)
             + list(losses or []))
    if cold:
        lines.append("radar: cold start — no prior snapshot, full board")
    if shown:





        lines.append(mrs._untrusted.flat_note("MR titles"))
        lines.extend(shown)
        lines.append("")
        lines.append(footer)
    elif cold:



        lines.append("All open MRs in this population are excluded."
                     if excluded else "No open MRs.")
        lines.append("")
        lines.append(footer)
    elif departed:

        lines.append(f"radar: no rows changed | {footer}")
    else:
        lines.append(f"radar: no change | {footer}")
    lines.extend(notes or [])
    return lines


def _no_watch(source: str, scope: str, only: list[str] | None = None) -> str:







    return "failed"


def radar_report(options: dict | None = None) -> tuple[list[str], bool]:























    options = options or {}
    watch = options.get("_watch") or _no_watch
    multi = resolve_filter(str(options.get("_arg") or ""))

    open_mrs = live_open_mrs(multi)

    open_iids = [str(m.get("iid")) for m in open_mrs if m.get("iid") is not None]
    watched = mrs._watched_iids(transport.STATE_DIR)

    states = read_state_files()
    pruned = prune_terminal(states, watched)
    drifted = drift({i: s for i, s in states.items() if i not in set(pruned)})

    healed, uncovered, refused = heal(open_iids, watched)
    covered = watched | set(healed)

    scope = feed_scope(multi)
    feed = watch(FEED_SOURCE, scope, feed_only())
    feed_err = feed_error(scope) if feed == "alive" else ""


    blind = feed_blind(scope) if feed == "alive" else ""

    exclusions, excl_problems = read_exclusions()
    excluded, excl_lines = resolve_exclusions(open_mrs, exclusions, covered)




    label = f"scope {filter_string(multi)}"
    if multi == default_filter():
        label += " (default)"
    previous = read_snapshot(multi)
    other_scopes = other_feed_scopes(scope)






    per_page = int(mrs._get_config().get("per_page") or 0)
    page_capped = bool(per_page) and len(open_mrs) >= per_page
    departed = _departed(previous, open_mrs)
    stale_after = options.get("stale_running_minutes", STALE_RUNNING_MINUTES)
    try:
        stale_after = float(stale_after)
    except (TypeError, ValueError):
        stale_after = STALE_RUNNING_MINUTES
    stamped_at = snapshot.now_iso()
    lines = render(open_mrs, covered, healed, drifted, pruned, uncovered, previous,
                   feed, feed_err, label, excluded, excl_problems + excl_lines,
                   other_scopes, loss_warnings(healed, refused), page_capped,
                   now=stamped_at, stale_running_minutes=stale_after,
                   feed_blind=blind)



    prev_entries: dict[str, Any] = (previous or {}).get("mrs", {}) or {}
    write_snapshot(
        {str(m.get("iid")): snapshot.stamp(_snap_entry(m),
                                           prev_entries.get(str(m.get("iid"))),
                                           stamped_at)
         for m in open_mrs if m.get("iid") is not None},
        multi,
    )








    healthy = not (uncovered or other_scopes or feed_err or departed or blind
                   or mrs._unchecked_count(open_mrs)
                   or feed in ("failed", "capped"))
    return lines, healthy


def radar_state(options: dict | None = None) -> list[str]:








    options = options or {}
    try:
        multi = resolve_filter(str(options.get("_arg") or ""))
    except RadarError as exc:



        return [f"  filter    : REFUSED — {exc}"]
    scope = feed_scope(multi)
    out = [f"  filter    : {filter_string(multi)}"
           f"{' (default)' if multi == default_filter() else ''}"]

    path = _snapshot_path(multi)
    previous = read_snapshot(multi)
    out.append(f"  snapshot  : {path} — "
               + (f"{len((previous or {}).get('mrs') or {})} MR(s)"
                  if previous is not None else "absent (cold start next run)"))

    pid, pid_refusal = transport.read_pid_checked(FEED_SOURCE, scope)
    err = feed_error(scope)



    out.append(f"  feed      : scope {scope!r}, pid "
               f"{pid or pid_refusal or 'none recorded'}"
               f"{f' — last error: {err}' if err else ''}")



    blind = feed_blind(scope)
    if blind:




        out.append(f"  feed sight: last poll could NOT establish the "
                   f"population — {blind}")
    for other in other_feed_scopes(scope):
        out.append(f"  feed ALSO : scope {other!r} is live and is NOT on this board")

    watched = sorted(mrs._watched_iids(transport.STATE_DIR))
    out.append(f"  watchers  : {', '.join('!' + i for i in watched) or 'none'}")

    exclusions, problems = read_exclusions()
    out.append(f"  exclusions: {len(exclusions)} configured"
               f"{f', {len(problems)} refused' if problems else ''}")
    return out
