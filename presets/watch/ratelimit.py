"""Rate-limit detection and budget math shared by the GitHub-backed watch
sources (#2509).

The whole fleet on one machine authenticates through the same `gh` keyring
token, whatever channel or repository each poller belongs to -- so the
5000 req/h core budget `gh api rate_limit` reports on is a fact about the
*token*, not about any one poller. This module holds the two things every
gh-backed source needs and none of them had before #2509:

  - telling a rate-limit failure apart from any other `*_unreachable`
    reading, from the error string alone (`is_rate_limit_error`);
  - the arithmetic `watches` prints the fleet's projected request rate
    with (`projected_requests_per_hour`), and the one `gh api rate_limit`
    read that grounds it against the real budget (`read_rate_limit`).

Nothing here spawns a poller or writes a state file. Every function is a
pure read (of the argument it was given) or one `gh` call, and every
`gh`-calling function returns `(value, "")` on success and `(None-ish,
why)` on failure -- the third state this whole preset keeps to, because
"could not tell" read as "clean" is the defect #2509 itself is one
instance of, one layer up.
"""
from __future__ import annotations

import json
import subprocess
import time
from typing import Any

#: Substrings of a poller's own `error` string (already produced by
#: `presets/github/run.py::_format_error` or `presets/github/branch.py`'s
#: own copy, both containing "rate limit" or "429" in their rendered
#: sentence) that mean this specific failure was the shared token's budget,
#: not a genuine outage. Read on the failure path only, lower-cased first --
#: never on a successful poll, and never used to reclassify anything besides
#: choosing whether to attach `retry_after`.
RATE_LIMIT_MARKERS = ("rate limit", "429")


def is_rate_limit_error(error: str) -> bool:
    """Whether a poller's own `error` string reads as a rate-limit failure.

    A plain substring check on the *poller's own words*, not a second
    classification of raw `gh` stderr -- every caller here already has an
    `error` string that went through `_format_error` (or branch.py's own
    copy), and this only asks which of those already-classified sentences
    this one is.
    """
    low = (error or "").lower()
    return any(marker in low for marker in RATE_LIMIT_MARKERS)


def _run_gh(args: list[str], timeout: float) -> tuple[str | None, int | None, str]:
    """(stdout, returncode, why-not). `returncode is None` means it never ran."""
    try:
        r = subprocess.run(["gh", *args], capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None, None, "gh not found -- install from https://cli.github.com"
    except subprocess.TimeoutExpired:
        return None, None, f"gh {' '.join(args)} timed out"
    except (OSError, subprocess.SubprocessError) as e:
        return None, None, f"gh {' '.join(args)} could not run: {e}"
    if r.returncode != 0:
        return None, r.returncode, (r.stderr or "gh failed with no stderr").strip()
    return r.stdout, r.returncode, ""


def reset_iso(epoch: Any) -> str:
    """A `resources.<kind>.reset` epoch second as `%Y-%m-%dT%H:%M:%SZ` UTC.

    `""` on anything that is not a real epoch -- never a guessed timestamp.
    """
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(epoch)))
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def read_rate_limit(timeout: float = 10.0) -> tuple[dict[str, Any] | None, str]:
    """`gh api rate_limit`'s core+graphql remaining/limit/reset, or (None, why).

    One call. GitHub's own docs say checking the rate limit does not itself
    count against it, so this is safe to call from the failure path of a
    poller that is already throttled, and from `watches` on every render.
    """
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
    """The core resource's own reset time, as ISO8601 UTC, or (None, why).

    Used by a poller that just hit the rate-limit marker, to attach
    `retry_after` to the `*_unreachable` event it is about to emit -- a
    second `gh api rate_limit` read rather than reusing `read_rate_limit`'s
    output, because a poller process never called that: it only has one
    failed `gh` call and a string.
    """
    limits, why = read_rate_limit(timeout=timeout)
    if limits is None:
        return None, why
    core = limits.get("core")
    if not core or not core.get("reset_iso"):
        return None, "gh api rate_limit had no usable core.reset"
    return core["reset_iso"], ""


def unreachable_extra(error: str, timeout: float = 10.0) -> dict[str, str]:
    """`{}` normally; `{"retry_after": iso}` for a rate-limit-shaped failure
    whose reset could actually be read.

    Never guesses: a rate-limit-shaped error whose `gh api rate_limit`
    lookup itself fails (the token really is dead, not just throttled, or
    `gh` is unreachable for an unrelated reason) still returns `{}` -- the
    caller's own `error` string is the record of that, and this must not
    manufacture a `retry_after` nothing established.
    """
    if not is_rate_limit_error(error):
        return {}
    iso, _why = fetch_reset_iso(timeout=timeout)
    if not iso:
        return {}
    return {"retry_after": iso}


#: Every source this preset ships whose `poll()` calls the GitHub REST or
#: GraphQL API and so draws on the one per-token budget `read_rate_limit`
#: reports on. Everything else under `presets/watch/sources/` authenticates
#: elsewhere (GitLab, Slack, Bluesky, dev.to) or not at all, and contributes
#: nothing to this bucket.
GH_SOURCES = frozenset({
    "gh-branch", "gh-run", "github-pr", "github-pr-feed", "github-issue-feed",
})

#: How many `gh`/API calls one poll of each source costs today -- a
#: hand-kept estimate, not an instrumented count. Each source's own `poll()`
#: (or the module it composes, for `gh-branch`) is the derivation; this
#: table only summarizes it and can drift from it the same way any other
#: comment can. `gh-branch` also makes one parallel `_jobs_for` call per run
#: selected on the watched ref's sha (`branch._jobs_for`, fetched through a
#: `ThreadPoolExecutor`) -- not counted per-run here, so a ref with several
#: concurrent workflow runs undercounts against this table, never overcounts.
CALLS_PER_TICK = {
    "gh-branch": 3,        # _head_commit, _run_list, _repo_identity
    "gh-run": 1,            # gh run view
    "github-pr": 1,         # gh pr view
    "github-pr-feed": 1,    # gh pr list
    "github-issue-feed": 1,  # gh issue list / search
}
DEFAULT_CALLS_PER_TICK = 1


def projected_requests_per_hour(pollers: int, calls_per_tick: int, interval: int) -> float:
    """`pollers x calls-per-tick x 3600/interval` -- the issue's own formula.

    `0.0` for any non-positive input rather than raising or dividing by
    zero: "no interval recorded" must read as "nothing to project", never
    as "infinite rate", which a bare `3600 / interval` would produce for
    `interval == 0`.
    """
    if pollers <= 0 or calls_per_tick <= 0 or interval <= 0:
        return 0.0
    return pollers * calls_per_tick * (3600.0 / interval)


def fleet_gh_poller_counts(census: dict[str, Any]) -> dict[str, int]:
    """{source: poller count} for every `GH_SOURCES` poller the process scan
    saw, summed across `census["mine"]`, every channel in `census["other"]`,
    and `census["unknown"]` -- the fleet-wide, cross-channel view #2509 asks
    for, built from the scan `presets/watch/transport.poller_census` already
    runs for `watches`' own foreign-poller disclosure, not a second scan.

    A poller for a source outside `GH_SOURCES` (GitLab, Slack, Bluesky,
    dev.to) contributes nothing: it draws on a different budget, or none.
    """
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
) -> tuple[float, dict[str, int]]:
    """(projected total requests/hour, {source: poller count}), fleet-wide.

    `interval_by_source` lets a caller that knows each source's actual
    `INTERVAL` (including a fleet-wide override, #2509's third request)
    pass it in; a source missing from it falls back to `default_interval` --
    30s, every `GH_SOURCES` member's own shipped default -- rather than
    failing the whole projection over one source this call could not
    resolve an interval for.
    """
    counts = fleet_gh_poller_counts(census)
    total = 0.0
    for source, n in counts.items():
        interval = (interval_by_source or {}).get(source) or default_interval
        calls = CALLS_PER_TICK.get(source, DEFAULT_CALLS_PER_TICK)
        total += projected_requests_per_hour(n, calls, interval)
    return total, counts


def render_budget_lines(
    rate_limit: dict[str, Any] | None,
    rate_limit_why: str,
    projected: float,
    counts: dict[str, int],
) -> list[str]:
    """The `watches` budget section -- plain strings, no `watches: ` prefix
    (the caller adds that, same as every other section of that board).

    Three states for the `gh api rate_limit` read itself, not two: a read
    that failed prints why rather than omitting the section, so an operator
    cannot mistake "could not check" for "nothing to report". The WARN line
    is the one positive control this function exists to make testable in
    isolation: it fires when (and only when) `projected` exceeds the core
    limit this read actually returned.
    """
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
    return lines

