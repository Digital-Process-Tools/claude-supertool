





















from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))  
sys.path.insert(0, str(Path(__file__).parent))  
import _untrusted  
import naming  








RATE_LIMIT_MARKERS = ("rate limit", "429")


def is_rate_limit_error(error: str) -> bool:








    low = (error or "").lower()
    return any(marker in low for marker in RATE_LIMIT_MARKERS)


def _run_gh(args: list[str], timeout: float) -> tuple[str | None, int | None, str]:

    try:
        r = subprocess.run(["gh", *args], capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None, None, "gh not found -- install the GitHub CLI"
    except subprocess.TimeoutExpired:
        return None, None, f"gh {' '.join(args)} timed out"
    except (OSError, subprocess.SubprocessError) as e:
        return None, None, f"gh {' '.join(args)} could not run: {e}"
    if r.returncode != 0:
        return None, r.returncode, (r.stderr or "gh failed with no stderr").strip()
    return r.stdout, r.returncode, ""


def reset_iso(epoch: Any) -> str:




    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(epoch)))
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def read_rate_limit(timeout: float = 10.0) -> tuple[dict[str, Any] | None, str]:






    stdout, returncode, why = _run_gh(["api", "rate_limit"], timeout)
    if stdout is None:
        return None, why
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return None, "gh api rate_limit returned unparseable JSON"
    resources = data.get("resources") if isinstance(data, dict) else None
    if not isinstance(resources, dict):
        return None, "gh api rate_limit response had no 'resources' object"
    out: dict[str, Any] = {}
    for key in ("core", "graphql"):
        entry = resources.get(key)
        if isinstance(entry, dict):
            out[key] = {
                "remaining": entry.get("remaining"),
                "limit": entry.get("limit"),
                "reset": entry.get("reset"),
                "reset_iso": reset_iso(entry.get("reset")),
            }
    if not out:
        return None, "gh api rate_limit response named neither core nor graphql"
    return out, ""


def fetch_reset_iso(timeout: float = 10.0) -> tuple[str | None, str]:








    limits, why = read_rate_limit(timeout=timeout)
    if limits is None:
        return None, why
    core = limits.get("core")
    if not core or not core.get("reset_iso"):
        return None, "gh api rate_limit had no usable core.reset"
    return core["reset_iso"], ""


def unreachable_extra(error: str, timeout: float = 10.0) -> dict[str, str]:









    if not is_rate_limit_error(error):
        return {}
    iso, _why = fetch_reset_iso(timeout=timeout)
    if not iso:
        return {}
    return {"retry_after": iso}







GH_SOURCES = frozenset({
    "gh-branch", "gh-run", "github-pr", "github-pr-feed", "github-issue-feed",
})









CALLS_PER_TICK = {
    "gh-branch": 3,        
    "gh-run": 1,            
    "github-pr": 1,         
    "github-pr-feed": 1,    
    "github-issue-feed": 1,  
}
DEFAULT_CALLS_PER_TICK = 1


def projected_requests_per_hour(pollers: int, calls_per_tick: int, interval: int) -> float:







    if pollers <= 0 or calls_per_tick <= 0 or interval <= 0:
        return 0.0
    return pollers * calls_per_tick * (3600.0 / interval)


def fleet_gh_poller_counts(census: dict[str, Any]) -> dict[str, int]:









    counts: dict[str, int] = {}

    def _tally(slots: dict[tuple[str, str], list[int]]) -> None:
        for (source, _watcher_id), pids in slots.items():
            if source in GH_SOURCES:
                counts[source] = counts.get(source, 0) + len(pids)

    _tally(census.get("mine", {}) or {})
    for slots in (census.get("other", {}) or {}).values():
        _tally(slots)
    _tally(census.get("unknown", {}) or {})
    return counts


def fleet_projected_requests_per_hour(
    census: dict[str, Any],
    interval_by_source: dict[str, int] | None = None,
    default_interval: int = 30,
    calls_per_tick_by_source: dict[str, int] | None = None,
) -> tuple[float, dict[str, int]]:































    counts = fleet_gh_poller_counts(census)
    total = 0.0
    for source, n in counts.items():
        interval = (interval_by_source or {}).get(source) or default_interval
        override = (calls_per_tick_by_source or {}).get(source)
        calls = (override if override is not None
                 else CALLS_PER_TICK.get(source, DEFAULT_CALLS_PER_TICK))
        total += projected_requests_per_hour(n, calls, interval)
    return total, counts


def render_budget_lines(
    rate_limit: dict[str, Any] | None,
    rate_limit_why: str,
    projected: float,
    counts: dict[str, int],
    active_errors: list[tuple[str, str, str]] | None = None,
    unreadable_states: list[tuple[str, str, str]] | None = None,
) -> list[str]:






































    lines: list[str] = []
    if not counts:
        lines.append("GitHub API budget: no gh-backed pollers seen on this machine.")
        return lines
    named = ", ".join(f"{n} {source}" for source, n in sorted(counts.items()))
    lines.append(f"GitHub API budget: {named} — projected "
                 f"{projected:.0f} req/h across every channel this scan saw.")
    if rate_limit is None:
        lines.append(f"  gh api rate_limit could not be read ({rate_limit_why}) — "
                     f"the projection above cannot be checked against the real "
                     f"budget.")
        return lines
    core = rate_limit.get("core")
    if not core:
        lines.append("  gh api rate_limit answered with no 'core' entry — cannot "
                     "check the projection against it.")
        return lines
    remaining, limit, reset_iso_str = (core.get("remaining"), core.get("limit"),
                                       core.get("reset_iso"))
    lines.append(f"  core: {remaining}/{limit} remaining, resets {reset_iso_str or '?'}")
    graphql = rate_limit.get("graphql")
    if graphql:
        lines.append(f"  graphql: {graphql.get('remaining')}/{graphql.get('limit')} "
                     f"remaining, resets {graphql.get('reset_iso') or '?'}")
    if isinstance(limit, (int, float)) and limit > 0 and projected > limit:
        lines.append(f"  WARN: projected {projected:.0f} req/h exceeds the core "
                     f"limit of {limit} — this machine's own pollers can empty "
                     f"the budget on their own, before any interactive session "
                     f"spends a call. Use a fleet-wide interval override "
                     f"(SUPERTOOL_WATCH_INTERVAL) or unwatch some of the "
                     f"{sum(counts.values())} gh-backed pollers listed above.")
    if active_errors and isinstance(remaining, (int, float)) and remaining > 0:
        example_source, example_id, example_error = active_errors[0]
        lines.append(
            f"  DISAGREEMENT: {len(active_errors)} gh-backed poller(s) are "
            f"currently failing with a rate-limit-shaped error (e.g. "
            f"{_untrusted.flat(example_source)}:{_untrusted.flat(example_id)} — "
            f"{_untrusted.flat(example_error)!r}) while this core reading shows "
            f"{remaining} of {limit} still free. GitHub's secondary rate limit "
            f"(abuse-detection throttling) is not reported by `gh api "
            f"rate_limit` or by any response header at all — a healthy-looking "
            f"core budget here does not mean the fleet is not being throttled. "
            f"Trust the poller's own error over this line.")
    if unreadable_states:
        first_why = unreadable_states[0][2]
        lines.append(
            f"  {len(unreadable_states)} gh-backed poller state file(s) could "
            f"not be read ({_untrusted.flat(first_why)}) — a rate-limit "
            f"disagreement on those could not be checked, which is not the "
            f"same as there being none.")
    return lines


def active_gh_rate_limit_errors(
    states: dict[tuple[str, str], dict[str, Any]],
) -> list[tuple[str, str, str]]:


















    out: list[tuple[str, str, str]] = []
    for (source, watcher_id), state in states.items():
        if source not in GH_SOURCES:
            continue
        error = (state or {}).get("error") or ""
        if is_rate_limit_error(error):
            out.append((source, watcher_id, error))
    return out


def gh_branch_calls_per_tick(workflow_count: int) -> int:


















    return 3 + max(0, workflow_count)


def workflow_file_count(repo_root: str) -> tuple[int | None, str]:























    wf_dir = os.path.join(repo_root, ".github", "workflows")
    names, state, why = naming.state_dir_listing(wf_dir)
    if state == naming.STATE_DIR_UNREADABLE:
        return None, why or f"{wf_dir} could not be listed"
    count = sum(1 for n in names if n.endswith((".yml", ".yaml")))
    return count, ""

