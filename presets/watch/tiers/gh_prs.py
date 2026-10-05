#!/usr/bin/env python3


























































































































from __future__ import annotations

import concurrent.futures
import glob
import importlib.util
import json
import os














import subprocess  
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).parent
_WATCH = _HERE.parent

sys.path.insert(0, str(_WATCH))
import dispatcher  
import naming  
import sourcepath  
import transport  

sys.path.insert(0, str(_WATCH.parent))
import _checks  
import _filter_tokens  
import _pr_board  
import _repo_target  
import _st_hint  
import _untrusted  


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prs = _load("radar_github_prs", _WATCH.parent / "github" / "prs.py")
pr = _load("radar_github_pr", _WATCH.parent / "github" / "pr.py")
branch = _load("radar_github_branch", _WATCH.parent / "github" / "branch.py")
snapshot = _load("radar_snapshot", _HERE / "_snapshot.py")





_auth_probe = _load("radar_auth_probe", _WATCH.parent / "_auth_probe.py")





sys.path.insert(0, str(_HERE))
import _radar_errors  




RadarError = _radar_errors.RadarError
RadarUnreachable = _radar_errors.RadarUnreachable
RadarUnconfigured = _radar_errors.RadarUnconfigured




NOT_AUTHENTICATED_MARKERS = _auth_probe.NOT_AUTHENTICATED_MARKERS

SOURCE = prs.WATCH_SOURCE
SNAPSHOT_PREFIX = "supertool-radar-gh-prs"










KNOWN_FILTERS = {"author", "assignee", "reviewer", "label", "state"}















KNOWN_FLAGS: set[str] = set()













VALUE_DOMAINS: dict[str, object] = {"state": prs._STATES}




RECONCILE_CAP = 6








STALE_RUNNING_MINUTES = 240

RADAR_OPTIONS = {"quiet_when_healthy", "default_branch", "reconcile_cap",
                 "stale_running_minutes", "pr_exclude_events"}













PR_EXCLUDE_OPTION = "pr_exclude_events"


def source_event_keys(source: str = SOURCE) -> list[str]:









    poller, _origin = sourcepath.find(source)
    if poller is None:
        raise RadarError(f"could not resolve watch source {source!r} to read "
                         f"its events.json -- not shipped and not on "
                         f"{sourcepath.PATH_ENV}")
    with open(poller.parent / "events.json", encoding="utf-8") as f:
        return [str(e["key"]) for e in json.load(f).get("events", [])]


def exclude_events(raw: object) -> list[str]:









    if raw is None or raw == "" or raw == []:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RadarError(
                f"{PR_EXCLUDE_OPTION} is a string that is not a JSON list "
                f"({exc.msg}): {raw!r}. Write it as a JSON array of event "
                f"keys, e.g. {json.dumps(['comment_added'])}.") from None
    if not isinstance(raw, list) or not all(isinstance(k, str) for k in raw):
        raise RadarError(
            f"{PR_EXCLUDE_OPTION} must be a list of event-key strings; got "
            f"{type(raw).__name__}: {raw!r}.")
    vocabulary = source_event_keys()
    unknown = sorted(set(raw) - set(vocabulary))
    if unknown:
        raise RadarError(
            f"{PR_EXCLUDE_OPTION} names event key(s) {SOURCE} cannot emit: "
            f"{', '.join(unknown)}. Nothing was healed. Valid keys, from "
            f"sources/{SOURCE}/events.json: {', '.join(vocabulary)}.")
    return [k for k in vocabulary if k in set(raw)]


def poller_only(excluded: list[str]) -> list[str]:






    if not excluded:
        return []
    drop = set(excluded)
    return [k for k in source_event_keys() if k not in drop]



RADAR_QUIET_DEFAULT = False






















GH_RC_NO_CREDENTIALS = 4



































_UNREACHABLE_MARKERS = (
    "not logged in",
    "http 401",
    "rate limit",
    "http 403",

    "dial tcp",
    "no such host",
    "connection refused",
    "connection reset",
    "network is unreachable",
    "i/o timeout",
    "tls handshake timeout",
    "client.timeout",
    "error connecting to",
)


def _unreachable(err: str) -> bool:

    low = err.lower()
    return any(marker in low for marker in _UNREACHABLE_MARKERS)






def resolve_filter(arg: str = "") -> dict[str, str]:


































    arg = (arg or "").strip()
    filters, _flags, unknown = _filter_tokens.parse(
        arg, KNOWN_FILTERS, KNOWN_FLAGS)
    if unknown:
        named = ", ".join(
            f"{t.partition('=')[0]}=" if "=" in t else t for t in unknown
        )





        flag_clause = (
            f"known flags: {', '.join(sorted(KNOWN_FLAGS))}"
            if KNOWN_FLAGS else "this tier accepts no flags at all"
        )
        raise RadarError(
            f"radar: gh-prs tier cannot honour {named!r}. Known filters: "
            f"{', '.join(sorted(KNOWN_FILTERS))}; {flag_clause}. Refusing "
            f"rather than running the query without it — an ignored filter "
            f"returns the whole board and reads as though everything matched."
        )
    bad = _filter_tokens.bad_values(filters, VALUE_DOMAINS)
    if bad:
        raise RadarError("radar: gh-prs tier " + _filter_tokens.value_error(bad))
    return filters


def filter_string(filters: dict[str, str]) -> str:

    return ",".join(f"{k}={v}" for k, v in sorted(filters.items()))


def scope_label(filters: dict[str, str], repo: str) -> str:

















    spelled = filter_string(filters)
    if not spelled:
        return f"scope every author (default) on {repo}"
    return f"scope {spelled} on {repo}"


def repo_name() -> str:

    return _repo_target.effective_slug(timeout=20) or "?"






def live_open_prs(filters: dict[str, str]) -> list[dict]:














    cfg = prs._get_config()
    cmd = prs._build_list_cmd(filters, cfg["per_page"])






    data, msg, returncode, raw_stderr = _pr_board.run_pr_list(cmd, timeout=30)
    if data is None:
        if returncode is None:



            raise RadarUnreachable(msg)
        if returncode == 0:






            raise RadarError(msg)









        err = _untrusted.flat(raw_stderr) or "unknown error"
        if returncode < 0:



















            raise RadarUnreachable(
                f"gh pr list did not finish before it answered (returncode "
                f"{returncode}, consistent with a killing signal on "
                f"POSIX — not established on Windows, see #1871): {err}")
        if returncode == GH_RC_NO_CREDENTIALS:



            raise RadarUnconfigured(
                "gh has no credentials in this environment, so it refused "
                "before making a request: " + err)
        low = err.lower()
        if _auth_probe.says_not_authenticated(err):



            raise RadarUnreachable(
                f"gh says this request was not authenticated (exit "
                f"{returncode}): {err}. Run: gh auth login")
        if "rate limit" in low or "http 403" in low:
            raise RadarUnreachable(
                f"gh refused the query (rate limit or permission, exit "
                f"{returncode}): {err}")
        if _unreachable(err):
            raise RadarUnreachable(
                f"gh could not reach the API (exit {returncode}): {err}")





        raise RadarError(
            f"gh pr list did not answer, and nothing in its output says why "
            f"(exit {returncode}): {err}")
    prs._annotate(data)
    return data






def _reconcile_one(p: dict) -> tuple[str, list[str]]:

    return pr._reconcile_checks(p)


def verify_green(open_prs: list[dict], cap: int = RECONCILE_CAP) -> list[str]:







    greens = [p for p in open_prs if p.get("_checks") == "success"]
    if not greens or cap <= 0:
        return ([f"radar: NOTE — green PRs are not being reconciled against "
                 f"their declared legs (reconcile_cap={cap}); a short rollup "
                 f"is indistinguishable from a complete one here."]
                if greens else [])
    lines: list[str] = []
    for p in greens[:cap]:
        marker, detail = _reconcile_one(p)
        if marker:
            p["_unverified"] = marker
            lines.append(
                f"radar: WARNING — #{p.get('number')} shows every check green, "
                f"but the tally could not be squared with what its runs declare "
                f"({marker}), so whether these are all the legs is UNKNOWN.")
            lines.extend(detail)
    if len(greens) > cap:
        lines.append(
            f"radar: WARNING — {len(greens) - cap} of {len(greens)} green PRs "
            f"were not reconciled against their declared legs (reconcile_cap is "
            f"{cap}): a short rollup among them is indistinguishable from a "
            f"complete one. Raise it in the tier's options.")
        for p in greens[cap:]:
            p["_unverified"] = "not reconciled"
    return lines


def unchecked(open_prs: list[dict]) -> list[str]:






    return [str(p.get("number")) for p in open_prs
            if not str(p.get("_checks") or "") or p.get("_unverified")]






def watch_coverage() -> set[str] | None:







    return prs._watched_numbers(transport.STATE_DIR)


def heal(numbers: list[str], watched: set[str] | None,
         watch, only: list[str] | None = None) -> tuple[list[str], list[str]]:
















    if watched is None:
        return [], []
    healed: list[str] = []
    uncovered: list[str] = []
    for number in [n for n in numbers if n not in watched]:
        status = watch(SOURCE, number, list(only or []))
        if status == "spawned":
            healed.append(number)
        elif status == "alive":
            continue
        else:
            uncovered.append(number)
    return healed, uncovered
















FEED_SOURCE = "github-pr-feed"

FEED_LABEL = {"alive": "feed ok", "spawned": "feed respawned", "failed": "feed DOWN",
              "capped": "feed DOWN (respawn capped)",
              "unknown": "feed coverage UNKNOWN (#673)"}







FEED_ONLY = ("pr_opened", "pr_merged", "pr_closed", "pr_left_feed", "prs_unreachable")


def feed_scope(filters: dict[str, str] | None = None) -> str:










    filters = {} if filters is None else filters
    return filter_string(filters) or "@open"


def feed_pid(scope: str = "@open") -> int:

    return transport.read_pid(FEED_SOURCE, scope)


def other_feed_scopes(scope: str) -> list[str]:








    return sorted({
        str(row.get("id") or "")
        for row in transport.list_active_pids()
        if row.get("source") == FEED_SOURCE and str(row.get("id") or "") != scope
    } - {""})


def feed_error(scope: str) -> str:







    state = transport.read_state(FEED_SOURCE, scope)


    return _untrusted.flat(str((state.get("last_error") or {}).get("message") or ""))






FEED_LOOKUP_UNAVAILABLE = "unavailable"


def feed_blind(scope: str) -> str:


















    state = transport.read_state(FEED_SOURCE, scope).get("source_state") or {}
    if not isinstance(state, dict) or state.get("lookup") != FEED_LOOKUP_UNAVAILABLE:
        return ""
    return (_untrusted.flat(str(state.get("error") or ""))
            or "(feed recorded no error message)")














BRANCH_SOURCE = "gh-branch"











BRANCH_ONLY = ("went_green", "went_not_green", "went_failed", "no_run",
               "unknown", "branch_unreachable")



BRANCH_LOOKUP_UNAVAILABLE = "unavailable"


def other_branch_scopes(scope: str) -> list[str]:























    return sorted({
        str(row.get("id") or "")
        for row in transport.list_active_pids()
        if row.get("source") == BRANCH_SOURCE and str(row.get("id") or "") != scope
    } - {""})


def branch_poller_error(scope: str) -> str:





    state = transport.read_state(BRANCH_SOURCE, scope)
    return _untrusted.flat(str((state.get("last_error") or {}).get("message") or ""))


def branch_poller_blind(scope: str) -> str:








    state = transport.read_state(BRANCH_SOURCE, scope).get("source_state") or {}
    if not isinstance(state, dict) or state.get("lookup") != BRANCH_LOOKUP_UNAVAILABLE:
        return ""
    return (_untrusted.flat(str(state.get("error") or ""))
            or "(branch poller recorded no error message)")


def _branch_poller_warnings(poller: str, err: str, others: list[str],
                            blind: str = "") -> list[str]:












    out = []
    if poller == "failed":
        out.append("radar: WARNING — default branch poller is down. A branch "
                   "that goes green or red after this point will not be "
                   "reported until the next radar tick.")
    elif poller == "capped":
        out.append("radar: WARNING — default branch poller has died too often "
                   "and is no longer being respawned. A branch that goes "
                   "green or red after this point will not be reported until "
                   "the next radar tick.")
    elif poller == "unknown":
        out.append("radar: WARNING — default branch poller coverage is "
                   "UNKNOWN for this board (#673, the same repo-blind pid "
                   "naming that leaves per-PR watch coverage unknown under a "
                   "repo target). Nothing was spawned; run radar from a clone "
                   "of that repo to get coverage back.")
    elif err:
        out.append(f"radar: WARNING — default branch poller is failing to "
                   f"poll: {err}")
    if blind:
        out.append(f"radar: WARNING — default branch poller is alive but "
                   f"could not establish the branch's state on its last "
                   f"poll: {blind}")
    for other in others:
        out.append(f"radar: WARNING — a default branch poller is also live "
                   f"for {other!r}, which this board is not reporting on. "
                   f"After a rename that kept the old ref, its green is "
                   f"indistinguishable from a real one for anyone still "
                   f"reading it — run "
                   f"{_st_hint.st_hint(f'unwatch:gh-branch:{other}')} to "
                   f"retire it.")
    return out


def default_branch_report(ref: str | None, repo: str,
                          watch=None) -> tuple[list[str], bool, bool]:




























    watch = watch or _no_watch
    if ref is None:
        ref = branch._repo_identity()[1]
    if not ref:
        return [], True, True

    poller = watch(BRANCH_SOURCE, ref, list(BRANCH_ONLY))
    poller_err = branch_poller_error(ref) if poller == "alive" else ""
    poller_blind = branch_poller_blind(ref) if poller == "alive" else ""
    poller_others = other_branch_scopes(ref)
    poller_lines = _branch_poller_warnings(poller, poller_err, poller_others,
                                           poller_blind)
    poller_ok = (poller not in ("failed", "capped", "unknown")
                and not poller_err and not poller_blind and not poller_others)









    sha, age, runs, err = _pr_board.head_and_runs(branch, ref)
    if err:
        return (poller_lines +
                [f"radar: {ref} — {branch.UNKNOWN}: {err} The default branch's "
                 f"state is not established; a red master looks exactly like "
                 f"this line being absent."], False, poller_ok)

    selected = branch.runs_on_sha(runs, sha)
    _prev_sha, prev_names = branch.previous_head(runs, sha)




    missing = branch.missing_workflows(prev_names, selected)

    fetched: dict = {}
    if selected:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(branch.JOB_WORKERS, len(selected))) as pool:
            fetched = dict(zip(selected, pool.map(
                lambda n: branch._jobs_for(branch._run_id(selected[n])), selected)))
    legs = {name: (None if jobs is None
                   else [_checks.github_state(j) for j in jobs])
            for name, jobs in fetched.items()}

    marker, shortfall = branch._reconcile(repo, selected, fetched)




    scope, scope_lines, unresolved = branch.scope_for(
        repo, sha, selected, age_secs=age, grace=branch._GRACE)
    state, sentence = branch.verdict(selected, legs, missing, sha, age,
                                     branch._GRACE, marker, scope=scope)

    lines = [f"radar: {ref} @ {sha[:7]} — {sentence}"]
    lines.extend(f"  {line}" for line in shortfall)
    for name in missing:
        lines.append(f"  {_untrusted.flat(name)} ran on the previous head and "
                     f"has no run on {sha[:7]} — that is not 'ran and passed'.")
    lines.extend(f"  {line}" for line in scope_lines)








    if state == branch.GREEN and not unresolved:
        lines = []









    could_tell = (state == branch.NOT_GREEN
                  or (state == branch.GREEN and not unresolved))
    return poller_lines + lines, could_tell, poller_ok






def snap_entry(p: dict) -> dict[str, Any]:









    return {
        "checks": str(p.get("_checks") or ""),
        "head_sha": str(p.get("headRefOid") or ""),
        "draft": bool(p.get("isDraft")),
        "mergeable": str(p.get("mergeable") or ""),
        "review": str(p.get("reviewDecision") or ""),
        "unverified": str(p.get("_unverified") or ""),
    }


def snapshot_key(filters: dict[str, str], repo: str) -> str:












    return snapshot.key({"repo": repo, "filters": dict(sorted(filters.items()))})


def snapshot_path(filters: dict[str, str], repo: str) -> str:
    return snapshot.path(SNAPSHOT_PREFIX, snapshot_key(filters, repo))


def _marks(p: dict, healed: set[str], uncovered: set[str],
           coverage_known: bool, stale_minutes: float = 0.0) -> str:
    out = []
    if p.get("_unverified"):
        out.append(f"[legs UNVERIFIED: {p['_unverified']}]")
    if stale_minutes:
        out.append(f"[{snapshot.unchanged_label(stale_minutes, str(p.get('_checks') or ''))}]")
    number = str(p.get("number", "?"))
    if not coverage_known:
        out.append("[watch?]")
    elif number in healed:
        out.append("[healed]")
    elif number in uncovered:
        out.append("[unwatched]")
    return ("  " + " ".join(out)) if out else ""


def _is_standing_problem(p: dict) -> bool:

    return (p.get("_checks") == "failed"
            or p.get("mergeable") == "CONFLICTING"
            or bool(p.get("_unverified"))
            or not str(p.get("_checks") or ""))


def _stale_running(p: dict, previous_entry: Any, threshold: float,
                   now: str | None = None) -> float:


















    if threshold <= 0 or str(p.get("_checks") or "") != "running":
        return 0.0
    mins = snapshot.unchanged_minutes(previous_entry, now)
    if mins is None or mins < threshold:
        return 0.0
    return mins


def _footer(open_prs: list[dict], covered: set[str] | None, healed: list[str],
            uncovered: list[str], gone: int, feed: str, label: str,
            unchecked_n: int, elided_n: int = 0,
            departed_capped: bool = False) -> str:








    counts: dict[str, int] = {}
    for p in open_prs:
        key = str(p.get("_checks") or "none")
        counts[key] = counts.get(key, 0) + 1
    parts = [label, f"{len(open_prs)} open"]
    if elided_n:
        parts.append(f"{elided_n} unchanged not shown")
    if counts.get("failed"):
        parts.append(f"{counts['failed']} failing")
    if counts.get("running"):
        parts.append(f"{counts['running']} running")
    green = counts.get("success", 0) - sum(
        1 for p in open_prs if p.get("_checks") == "success" and p.get("_unverified"))
    if green:
        parts.append(f"{green} green")
    if unchecked_n:
        parts.append(f"{unchecked_n} unchecked")
    if covered is None:
        parts.append("watch coverage UNKNOWN")
    else:
        parts.append(f"{len([p for p in open_prs if str(p.get('number')) in covered])} watched")
    if healed:
        parts.append(f"{len(healed)} healed")
    if uncovered:
        parts.append(f"{len(uncovered)} unwatched")
    if gone:



        parts.append(f"{gone} off this page" if departed_capped
                     else f"{gone} left this board")






    parts.append(f"discovery: {FEED_LABEL.get(feed, feed)}")
    return " | ".join(parts)


def _coverage_warning(covered: set[str] | None) -> list[str]:
    if covered is not None:
        return []
    return ["radar: WARNING — watch coverage is UNKNOWN for this board. Watch "
            "state is keyed by PR number with no repository (#673) and this "
            "board is about a repo target, so a live poller for #N cannot be "
            "told apart from #N of the clone it was started in. Nothing was "
            "healed; run radar from a clone of that repo to get coverage back."]


def _feed_warnings(feed: str, feed_err: str, others: list[str],
                   feed_blind: str = "") -> list[str]:





    out = []
    if feed == "failed":
        out.append("radar: WARNING — PR feed poller is down. New PRs will not be "
                   "discovered until the next radar tick.")
    elif feed == "capped":
        out.append("radar: WARNING — PR feed poller has died too often and is no "
                   "longer being respawned. New PRs will not be discovered until "
                   "the next radar tick.")
    elif feed == "unknown":
        out.append("radar: WARNING — PR feed coverage is UNKNOWN for this board "
                   "(#673, the same repo-blind pid naming that leaves per-PR "
                   "watch coverage unknown under a repo target). Nothing was "
                   "spawned; run radar from a clone of that repo to get "
                   "discovery back.")
    elif feed_err:
        out.append(f"radar: WARNING — PR feed poller is failing to poll: {feed_err}")
    if feed_blind:
        out.append(f"radar: WARNING — PR feed poller is alive but could not "
                   f"establish the population on its last poll: {feed_blind}")
    for other in others:
        out.append(f"radar: NOTE — a PR feed poller is also live on scope "
                   f"{other!r}, which this board did not resolve to. Its "
                   f"discoveries are not this board's.")
    return out


def _unchecked_warning(numbers: list[str], total: int) -> list[str]:

    if not numbers:
        return []
    shown = ", ".join(f"#{n}" for n in numbers[:8])
    if len(numbers) > 8:
        shown += f", +{len(numbers) - 8} more"
    return [f"radar: WARNING — {len(numbers)} of {total} PRs on this board have "
            f"no established check state ({shown}): unknown, not green, so a "
            f"failing one among them is indistinguishable from a passing one "
            f"here."]


def _departed(previous: dict | None, open_prs: list[dict]) -> list[str]:






    prev_entries: dict[str, Any] = (previous or {}).get("prs", {}) or {}
    live = {str(p.get("number")) for p in open_prs}
    return [n for n in prev_entries if n not in live]


def render(open_prs: list[dict], covered: set[str] | None, healed: list[str],
           uncovered: list[str], previous: dict | None, label: str,
           notes: list[str] | None = None,
           page_capped: bool = False,
           now: str | None = None,
           stale_running_minutes: float = STALE_RUNNING_MINUTES,
           feed: str = "alive", feed_err: str = "",
           other_feed_scopes: list[str] | None = None,
           feed_blind: str = "",
           excluded_events: list[str] | None = None) -> list[str]:












    cold = previous is None
    prev_entries: dict[str, Any] = (previous or {}).get("prs", {}) or {}
    healed_set, uncovered_set = set(healed), set(uncovered)
    coverage_known = covered is not None
    unchecked_numbers = unchecked(open_prs)

    shown = []
    elided: list[str] = []
    for p in sorted(open_prs, key=prs._sort_key):
        number = str(p.get("number", "?"))
        prev_entry = prev_entries.get(number)


        moved = snapshot.facts(prev_entry) != snap_entry(p)
        notable = number in healed_set or number in uncovered_set
        stale = _stale_running(p, prev_entry, stale_running_minutes, now)
        if cold or moved or notable or _is_standing_problem(p) or stale:
            shown.append(prs._row(p, covered,
                                  _marks(p, healed_set, uncovered_set,
                                         coverage_known, stale)))
        else:
            elided.append(number)

    departed = _departed(previous, open_prs)
    footer = _footer(open_prs, covered, healed, uncovered, len(departed), feed,
                     label, len(unchecked_numbers), len(elided), page_capped)






    elision = (snapshot.elided_note(elided, len(open_prs), "PRs", "#", "gh-prs")
               if shown else [])

    lines = (_coverage_warning(covered)
             + _feed_warnings(feed, feed_err, other_feed_scopes or [], feed_blind)
             + _unchecked_warning(unchecked_numbers, len(open_prs))
             + elision
             + snapshot.departed_note(departed, "PR", "#", "gh-pr:<number>",
                                      page_capped)
             + list(notes or []))
    if cold:
        lines.append("radar: cold start — no prior snapshot, full board")
    if shown:
        lines.append(_untrusted.flat_note("PR titles"))
        lines.extend(shown)
        lines.append("")
        lines.append(footer)
    elif cold:



        lines.append(f"No PRs matched — {label}.")
        lines.append("")
        lines.append(footer)
    elif departed:





        lines.append(f"radar: no rows changed | {footer}")
    else:
        lines.append(f"radar: no change | {footer}")
    if excluded_events:
        lines.append(_exclude_note(excluded_events))
    return lines


def _exclude_note(excluded: list[str]) -> str:








    return (f"pollers spawned from here exclude: {', '.join(excluded)} — a "
            f"github-pr poller already alive keeps the filter it was forked "
            f"with; `unwatch:github-pr:N` then `radar` re-forks #N with this one")


def _no_watch(source: str, scope: str, only: list[str] | None = None) -> str:


    return "failed"


def radar_report(options: dict | None = None) -> tuple[list[str], bool]:











    options = options or {}
    watch = options.get("_watch") or _no_watch
    filters = resolve_filter(str(options.get("_arg") or ""))


    excluded = exclude_events(options.get(PR_EXCLUDE_OPTION))

    repo = repo_name()
    open_prs = live_open_prs(filters)

    cap = options.get("reconcile_cap", RECONCILE_CAP)
    try:
        cap = int(cap)
    except (TypeError, ValueError):
        cap = RECONCILE_CAP
    notes = verify_green(open_prs, cap)

    numbers = [str(p.get("number")) for p in open_prs if p.get("number") is not None]
    watched = watch_coverage()
    healed, uncovered = heal(numbers, watched, watch, poller_only(excluded))
    covered = None if watched is None else watched | set(healed)





    scope = feed_scope(filters)
    if watched is not None:
        feed = watch(FEED_SOURCE, scope, list(FEED_ONLY))
        feed_err = feed_error(scope) if feed == "alive" else ""
        blind = feed_blind(scope) if feed == "alive" else ""
        other_scopes = other_feed_scopes(scope)
    else:
        feed, feed_err, blind, other_scopes = "unknown", "", "", []

    raw_ref = options.get("default_branch")
    branch_lines, branch_ok, branch_poller_ok = default_branch_report(
        None if raw_ref is None else str(raw_ref), repo, watch)

    label = scope_label(filters, repo)
    key = snapshot_key(filters, repo)
    previous = snapshot.read(SNAPSHOT_PREFIX, key, "prs")



    per_page = int(prs._get_config().get("per_page") or 0)
    page_capped = bool(per_page) and len(open_prs) >= per_page
    departed = _departed(previous, open_prs)
    stale_after = options.get("stale_running_minutes", STALE_RUNNING_MINUTES)
    try:
        stale_after = float(stale_after)
    except (TypeError, ValueError):
        stale_after = STALE_RUNNING_MINUTES
    stamped_at = snapshot.now_iso()
    lines = branch_lines + render(open_prs, covered, healed, uncovered,
                                  previous, label, notes, page_capped,
                                  now=stamped_at,
                                  stale_running_minutes=stale_after,
                                  feed=feed, feed_err=feed_err,
                                  other_feed_scopes=other_scopes,
                                  feed_blind=blind,
                                  excluded_events=excluded)
    prev_entries: dict[str, Any] = (previous or {}).get("prs", {}) or {}
    snapshot.write(SNAPSHOT_PREFIX, key,
                   {str(p.get("number")): snapshot.stamp(
                       snap_entry(p), prev_entries.get(str(p.get("number"))),
                       stamped_at)
                    for p in open_prs},
                   "prs")









    healthy = bool(branch_ok) and bool(branch_poller_ok) \
        and not uncovered and covered is not None \
        and not unchecked(open_prs) and not departed \
        and feed not in ("failed", "capped", "unknown") \
        and not feed_err and not blind and not other_scopes
    return lines, healthy






def radar_state(options: dict | None = None) -> list[str]:







    options = options or {}
    out: list[str] = []
    try:
        filters = resolve_filter(str(options.get("_arg") or ""))
    except RadarError as exc:
        return [f"  filter    : REFUSED — {exc}"]

    target = _repo_target.target()
    repo = str(target) if target else "(the cwd's clone — not resolved here, "\
                                     "that would be a call)"
    out.append(f"  filter    : "
               f"{filter_string(filters) or 'none — every author (default)'}")
    out.append(f"  repo      : {repo}")

    raw_ref = options.get("default_branch")
    out.append("  default br: " + ("(resolved at report time)" if raw_ref is None
                                   else (str(raw_ref) or "(off)")))
    try:
        excluded = exclude_events(options.get(PR_EXCLUDE_OPTION))
    except RadarError as exc:
        out.append(f"  pr events : REFUSED — {exc}")
    else:
        out.append("  pr events : " + (f"all but {', '.join(excluded)} (pollers "
                                       f"spawned from here)" if excluded
                                       else "all (no pr_exclude_events)"))

    path = snapshot_path(filters, str(target) if target else "?")
    if target:
        try:
            with open(path, encoding="utf-8") as f:
                rows = len((json.load(f).get("prs") or {}))
            out.append(f"  snapshot  : {path} — {rows} PR(s)")
        except (OSError, json.JSONDecodeError):
            out.append(f"  snapshot  : {path} — absent (cold start next run)")
    else:


        out.append(f"  snapshot  : under {naming.flat_path(transport.STATE_DIR)}/"
                   f"{SNAPSHOT_PREFIX}.*.snapshot.json — the exact key needs "
                   f"the repo name, which is a call, so it is not resolved here")

    prefix = f"supertool-watch-{SOURCE}__"
    pids = sorted(os.path.basename(p)[len(prefix):-len(".pid")]
                  for p in glob.glob(os.path.join(transport.STATE_DIR,
                                                  f"{prefix}*.pid")))
    if target:
        out.append(f"  pollers   : {len(pids)} pid file(s) — UNKNOWN whether they "
                   f"cover this repo (#673): {', '.join('#' + n for n in pids) or 'none'}")
    else:
        out.append(f"  pollers   : {', '.join('#' + n for n in pids) or 'none'}")





    scope = feed_scope(filters)
    if target:
        out.append(f"  feed      : scope {scope!r} — UNKNOWN whether a live pid "
                   f"here covers this repo (#673); nothing is spawned for it "
                   f"under a repo target")
    else:
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
    return out
