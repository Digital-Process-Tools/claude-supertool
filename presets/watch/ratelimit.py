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
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))  # for _untrusted
sys.path.insert(0, str(Path(__file__).parent))  # for naming, our own sibling
import _untrusted  # noqa: E402  (a poller's own error string is somebody else's text, #2525)
import naming  # noqa: E402  (the one directory-listing classifier every enumeration in this preset must use, #1502)

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
    calls_per_tick_by_source: dict[str, int] | None = None,
) -> tuple[float, dict[str, int]]:
    """(projected total requests/hour, {source: poller count}), fleet-wide.

    `interval_by_source` lets a caller that knows each source's actual
    `INTERVAL` (including a fleet-wide override, #2509's third request)
    pass it in; a source missing from it falls back to `default_interval` --
    30s, every `GH_SOURCES` member's own shipped default -- rather than
    failing the whole projection over one source this call could not
    resolve an interval for.

    `calls_per_tick_by_source` (#2525) lets a caller override
    `CALLS_PER_TICK`'s flat, hand-kept estimate with a measured number --
    `gh_branch_calls_per_tick(workflow_file_count(...))` for `gh-branch`,
    whose flat `3` this module's own comment already admitted undercounts.
    **Not wired into `watches` today** (self-review on #2525 found the one
    candidate source for it -- the calling process's own cwd -- is not a
    sound stand-in for "the repo every gh-branch poller in the fleet
    watches": a poller resolves its own target via `SUPERTOOL_REPO` at
    spawn time or the *spawning* process's cwd, either of which can differ
    from the cwd `watches` is run from later, and from each other across
    pollers on different channels. Applying one repo's workflow count to
    every gh-branch poller in the fleet can undercount a *different* one,
    which is the exact failure this override exists to close. Kept here,
    tested, as a building block for a future caller that can resolve each
    poller's own repo -- see `workflow_file_count`'s own docstring.

    A source explicitly present in `calls_per_tick_by_source` always wins,
    including a deliberate `0` -- checked with `is not None`, not truthiness,
    so an override of zero is not silently mistaken for "no override given".
    A source missing from it falls back to `CALLS_PER_TICK`, same shape as
    `interval_by_source` above.
    """
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
    """The `watches` budget section -- plain strings, no `watches: ` prefix
    (the caller adds that, same as every other section of that board).

    Three states for the `gh api rate_limit` read itself, not two: a read
    that failed prints why rather than omitting the section, so an operator
    cannot mistake "could not check" for "nothing to report". The WARN line
    is the one positive control this function exists to make testable in
    isolation: it fires when (and only when) `projected` exceeds the core
    limit this read actually returned.

    `active_errors` (#2525) is `[(source, id, error), ...]` for every
    GH_SOURCES poller whose own last-read state currently carries a
    rate-limit-shaped error -- `active_gh_rate_limit_errors`, below, builds
    it from the fleet's own state files. When it is non-empty *and* the core
    reading above shows real headroom, the two instruments disagree: GitHub
    enforces a *secondary* rate limit (abuse-detection throttling on request
    concurrency/burst rate, undocumented thresholds) entirely separately
    from the primary per-hour budget `gh api rate_limit` reports on, and
    GitHub's own docs say a secondary-limit rejection is not reflected in
    `/rate_limit` or in any response header at all -- so a full-looking core
    line here is never evidence that the fleet is not being throttled.
    `remaining == 0` is the one case this must NOT flag: there the core
    budget itself explains the failure, and printing a disagreement over a
    consistent reading would be the false positive this exists to avoid.
    `example_source`/`example_id`/`example_error` are a poller's own
    argv-derived id and error text, so they are flattened through
    `_untrusted.flat()` before they reach this board, the same convention
    every other row on this board already applies.

    `unreadable_states` (#2525, self-review finding) is
    `[(source, id, why), ...]` for a `GH_SOURCES` poller whose state file
    the caller could not read at all -- a symlink refusal, a corrupt or
    mid-write JSON file. Reported as its own caveat rather than folded
    silently into "no active error": a poller failing with a rate-limit
    error whose state file cannot be read right now must not read as
    "checked and clean", which is the same absence-as-clean shape this
    whole preset exists to stop reproducing one layer up.
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
    """`[(source, id, error), ...]` for every `GH_SOURCES` poller in `states`
    whose current state carries a rate-limit-shaped `error` right now (#2525).

    `states` maps `(source, watcher_id) -> state dict` -- the caller reads
    each poller's own state file fresh (`transport.read_state_checked`, not
    `read_state`: a caller building a *report* -- which `render_budget_lines`
    is -- must not have "could not read this file" collapse into "no error",
    the same distinction `transport.read_state`'s own docstring already
    draws for every other report call site in this preset), because the
    board rows this preset already builds carry only `last_event`'s event
    key, not the raw error text `is_rate_limit_error` classifies. A source
    outside `GH_SOURCES` (GitLab, Slack, ...) contributes nothing: it draws
    on a different budget, or none, and the same "rate limit" wording in an
    unrelated tool's error would be a false positive here. A state that
    could not be read at all is the caller's concern (see
    `dispatcher._active_gh_rate_limit_errors`'s `unreadable` half), not
    this function's: it only classifies what it was actually handed.
    """
    out: list[tuple[str, str, str]] = []
    for (source, watcher_id), state in states.items():
        if source not in GH_SOURCES:
            continue
        error = (state or {}).get("error") or ""
        if is_rate_limit_error(error):
            out.append((source, watcher_id, error))
    return out


def gh_branch_calls_per_tick(workflow_count: int) -> int:
    """3 fixed calls (`_head_commit`, `_run_list`, `_repo_identity`) plus one
    `_jobs_for` call per workflow selected on the watched sha (#2525).

    `CALLS_PER_TICK["gh-branch"]` used to be a flat `3` that this module's
    own comment already admitted undercounts by exactly this -- one
    `_jobs_for` call per selected run, never zero once any workflow has run
    on the watched sha. `workflow_count` is the number of workflow files in
    the watched repo's `.github/workflows/` (see `workflow_file_count`,
    below) -- an upper approximation of how many runs are typically selected
    on one commit, since a path-filtered workflow will not fire on every
    push. An upper approximation is the right direction to round in: this
    table exists to warn before the budget is empty, and rounding it down
    the way the flat `3` did is rounding toward the false negative.

    Never below the three fixed calls `_snapshot` always makes, regardless
    of how a caller's workflow count was derived -- a negative or garbage
    count must not project fewer requests than the poller's own floor.
    """
    return 3 + max(0, workflow_count)


def workflow_file_count(repo_root: str) -> tuple[int | None, str]:
    """How many `*.yml`/`*.yaml` files live under `repo_root/.github/workflows`,
    or `(None, why)` when that cannot be told (#2525).

    Each workflow file is a candidate producer of a run selected on the
    watched sha (`gh-branch._run_list` + `runs_on_sha`) -- not every one
    necessarily fires on a given push (path filters, branch filters), so
    this is an upper bound on `gh_branch_calls_per_tick`'s `workflow_count`,
    never an exact per-commit count.

    Goes through `naming.state_dir_listing`, the one directory-listing
    classifier every enumeration in this preset must use (#1502) -- caught
    by `tests/test_watch_state_dir_absent_1502.py`'s own sweep for a bare
    stdlib directory-listing call anywhere under `presets/watch/`, on the
    first version of this function, which called one directly. It is the
    only thing that can tell "this directory has nothing in it" from "this
    directory could not be read", the same three-states rule as every
    other read in this module. A repo whose `.github/workflows` **does not
    exist at all** is a complete, knowable answer here -- zero workflow
    files -- not a "could not tell": `STATE_DIR_ABSENT` returns
    `count = 0`. Only `STATE_DIR_UNREADABLE` (a permissions error, a file
    sitting where the directory should be) is the genuine could-not-tell
    case, and only that one returns `(None, why)`.
    """
    wf_dir = os.path.join(repo_root, ".github", "workflows")
    names, state, why = naming.state_dir_listing(wf_dir)
    if state == naming.STATE_DIR_UNREADABLE:
        return None, why or f"{wf_dir} could not be listed"
    count = sum(1 for n in names if n.endswith((".yml", ".yaml")))
    return count, ""

