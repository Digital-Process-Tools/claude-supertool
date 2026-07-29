#!/usr/bin/env python3
"""GitLab runner fleet state via glab CLI.

`glab api projects/:id/runners` answers "which runners exist", which is never
the question. The questions are "is anything stuck" and "why is my job not
starting" — and the fleet list alone cannot answer either, because a runner
that stopped polling an hour ago still reports `status: online` until GitLab's
offline threshold trips, and still reports `job_execution_status: idle`
because it is, technically, idle. Idle and dead look identical.

So this op joins three sources the fleet list keeps apart:

    runners list  ->  who exists, shared vs project
    runners/:id   ->  tags, run_untagged, last contact
    jobs (queue)  ->  what is waiting, and for which tags

and derives the one line you actually want:

    gl-runners           fleet table + queue depth + starvation warnings
    gl-runners:full      adds project scope, timeouts, runner managers
    gl-runners:queue     pending/running jobs grouped by the runner that can take them

STARVED is the finding this exists for: pending jobs whose tags match only
runners that have stopped heartbeating. Those jobs cannot move, no other
runner is allowed to take them, and nothing in the GitLab UI says so.
"""
from __future__ import annotations

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

# GitLab does not write contacted_at on every poll — it throttles the update.
# Measured on a live fleet: one runner's contacted_at stayed frozen at the same
# millisecond across 7 samples spanning 2 minutes, drifting to ~10 minutes of
# apparent staleness while the runner was healthy and the fleet was completing
# jobs throughout. A threshold below that granularity does not measure runner
# health, it measures GitLab's write policy — and it fired on 6 of 6 online
# runners when it was set to 300s. 30 minutes sits well clear of the observed
# throttle while still catching a genuinely wedged runner long before GitLab's
# own `stale` (~3 months) would.
_HEARTBEAT_WARN_SECONDS = 1800

# Staleness alone is not evidence: a fleet with nothing queued is allowed to be
# quiet. A job only counts toward starvation once it has waited longer than a
# normal scheduling delay, so a pipeline that started seconds ago is never
# mistaken for blocked work.
_STARVED_MIN_QUEUE_SECONDS = 300

# Completed work is the strongest liveness evidence available: a runner that
# finished jobs inside this window is alive, whatever contacted_at claims and
# whether or not it happens to be executing something at this instant. It also
# answers the capacity question — jobs per runner over a fixed window is what
# "is one of these doing all the work" actually means.
_THROUGHPUT_WINDOW_SECONDS = 1800

# The jobs endpoint returns newest-first by id, which orders by created_at —
# and finished_at is NOT monotonic with it. A test job created 09:17 and still
# running at 11:45 finishes long after jobs created an hour later. Stopping the
# scan the moment created_at leaves the window therefore misses exactly the
# long jobs whose completion is the most interesting evidence. So the scan
# reaches back a further allowance covering the longest plausible job, and
# filters on finished_at rather than on where it stopped.
_LONG_JOB_ALLOWANCE_SECONDS = 4 * 3600

# Hard cap so a busy fleet cannot turn one op into an unbounded crawl. Hitting
# it makes the counts a lower bound, which the caller is told about rather than
# left to assume — a truncated count that reads as complete is worse than no
# count at all.
_MAX_HISTORY_PAGES = 5

_MODES = {"full", "queue"}

_MAX_DETAIL_WORKERS = 8


def _format_error(stderr: str, resource: str) -> str:
    """Classify glab errors into actionable messages for LLMs."""
    s = stderr.lower()
    if "404" in s or "not found" in s or "could not resolve" in s:
        return f"ERROR: {resource} not found. Verify you're in the right repo."
    if "401" in s or "unauthorized" in s or "authenticate" in s or "bad token" in s or "token expired" in s:
        return "ERROR: glab not authenticated. Run: glab auth login"
    if "403" in s or "forbidden" in s:
        return (
            f"ERROR: permission denied reading {resource}. Instance-wide runner data "
            "needs admin; project-scoped runners need Maintainer."
        )
    return f"ERROR: glab failed reading {resource}: {stderr.strip()}"


def _parse_paginated_json(raw: str) -> list[dict]:
    """Parse glab's `--paginate` output — one JSON array per page, concatenated.

    glab emits each page's body back-to-back with no separator, e.g.
    `[{a}][{b},{c}]`. A single `json.loads` chokes on the second `[`.
    """
    decoder = json.JSONDecoder()
    merged: list[dict] = []
    idx = 0
    length = len(raw)
    while idx < length:
        while idx < length and raw[idx].isspace():
            idx += 1
        if idx >= length:
            break
        doc, end = decoder.raw_decode(raw, idx)
        if not isinstance(doc, list):
            raise ValueError("expected a JSON array per page")
        merged.extend(doc)
        idx = end
    return merged


def _api(endpoint: str, paginate: bool = False, timeout: int = 20):
    """One glab API call. Returns (data, error_message)."""
    cmd = ["glab", "api", endpoint]
    if paginate:
        cmd.append("--paginate")
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        return None, "ERROR: glab not found — install from https://gitlab.com/gitlab-org/cli"
    except subprocess.TimeoutExpired:
        return None, f"ERROR: glab timed out reading {endpoint}"

    if result.returncode != 0:
        return None, _format_error(result.stderr, endpoint)

    try:
        if paginate:
            return _parse_paginated_json(result.stdout), None
        return json.loads(result.stdout), None
    except (json.JSONDecodeError, ValueError):
        return None, f"ERROR: invalid JSON from glab for {endpoint}"


def _age_seconds(timestamp: str | None) -> float | None:
    """Seconds since an ISO-8601 GitLab timestamp, or None if unparseable."""
    if not timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - parsed).total_seconds()


def _human_age(seconds: float | None) -> str:
    """Compact age: 45s, 12m, 3h, 5d."""
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.0f}h"
    return f"{seconds / 86400:.0f}d"


def _fetch_details(runners: list[dict]) -> dict[int, dict]:
    """Per-runner detail (tags, contacted_at, run_untagged) fetched in parallel.

    The list endpoint omits tag_list and contacted_at, which are exactly the two
    fields the starvation check needs, so one call per runner is unavoidable.
    """
    def one(runner: dict) -> tuple[int, dict]:
        data, _err = _api(f"runners/{runner['id']}", timeout=15)
        return runner["id"], (data or {})

    workers = min(_MAX_DETAIL_WORKERS, max(1, len(runners)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(pool.map(one, runners))


def _can_serve(runner: dict, job_tags: list[str]) -> bool:
    """GitLab job-to-runner matching: a runner needs ALL of a job's tags.

    Untagged jobs only go to runners with run_untagged enabled.
    """
    runner_tags = set(runner.get("tag_list") or [])
    if not job_tags:
        return bool(runner.get("run_untagged"))
    return set(job_tags).issubset(runner_tags)


# Each annotator leaves a mark, so "annotated, found nothing" and "never
# annotated" are different records rather than the same falsy zero.
_LIVE_JOBS_MARK = "_live_jobs_checked"


class UnannotatedFleetError(RuntimeError):
    """A liveness question asked about records nobody gathered evidence for.

    Raised rather than answered, because the two wrong answers available here
    are both worse. Judging anyway means judging on `contacted_at` alone — the
    throttled field — which reported `runner_silent` on 6 of 6 healthy runners
    the one time it shipped. Defaulting to responsive means reporting an empty
    starvation list for a fleet nobody looked at, which is a false all-clear in
    a tool whose entire job is to notice a wedge GitLab denies.

    The callers already know how to carry a refusal: the watch dispatcher
    records a failed poll as `last_error` and emits no events, and radar turns
    a raised tier into `WARNING — tier failed` with the board not green. Both
    are the honest outcome — health UNKNOWN, said out loud.
    """


def _missing_annotations(runner: dict) -> list[str]:
    """Which evidence-gathering steps never ran for this record.

    "Annotated, found nothing" and "never annotated" have to be distinguishable,
    so each annotator leaves a mark: zero completed jobs is an observation, an
    absent `_recent_jobs` key is the absence of one.
    """
    missing = []
    if "_recent_jobs" not in runner:
        missing.append("annotate_recent_work")
    if not runner.get(_LIVE_JOBS_MARK):
        missing.append("annotate_live_jobs")
    return missing


def _is_responsive(runner: dict) -> bool:
    """Able to pick up queued work right now.

    Evidence in descending strength, and the order is the whole point:

    1. It finished jobs inside the throughput window. Work completed is proof
       no inference can beat — the runner demonstrably took work and returned
       results.
    2. `job_execution_status == "active"`. GitLab says it is executing a job at
       this instant.
    3. contacted_at age. Consulted last and at a deliberately loose threshold,
       because it is the throttled field: GitLab does not write it every poll,
       so a runner taking jobs continuously still reads minutes stale. Testing
       it first is how a fleet-wide false alarm gets built.

    1 and 2 require `annotate_recent_work` / `annotate_live_jobs` to have run;
    without them this degrades to 3 alone, which is exactly the broken version.
    So it refuses: an un-annotated record raises `UnannotatedFleetError` instead
    of getting a verdict. A liveness check that cannot see the throughput
    evidence has not observed a quiet runner, it has failed to look, and the
    caller that skipped the step is the only party able to fix it.

    The refusal is unconditional, checked ahead of the paused/offline
    disqualifiers. Those would answer correctly without annotation, and a guard
    that lets some un-annotated calls through is a guard a new caller can pass
    in review and in tests and still ship the fleet-wide alarm.
    """
    missing = _missing_annotations(runner)
    if missing:
        raise UnannotatedFleetError(
            f"liveness asked about runner {runner.get('id')} before "
            f"{' and '.join(missing)} ran. Annotate the fleet first — without "
            f"it the judgement falls back to contacted_at alone, the field "
            f"GitLab throttles, and a working fleet reads as wholly silent."
        )
    if runner.get("paused") or not runner.get("active", True):
        return False
    if runner.get("status") in {"offline", "stale", "never_contacted"}:
        return False
    if runner.get("_recent_jobs"):
        return True
    if runner.get("job_execution_status") == "active":
        return True
    age = _age_seconds(runner.get("contacted_at"))
    return age is not None and age <= _HEARTBEAT_WARN_SECONDS


def fetch_recent_finished(
    window_seconds: int = _THROUGHPUT_WINDOW_SECONDS,
) -> tuple[list[dict], bool]:
    """(jobs finished inside the window, truncated). ([], False) on failure.

    An empty list is safe here in a way it is not elsewhere: a missing history
    only costs the throughput evidence, and liveness falls through to the
    weaker tests below it rather than flipping a healthy runner to dead.

    `truncated` is True when the page cap was reached with more history still
    to read, so the counts are a floor rather than a total.
    """
    collected: list[dict] = []
    truncated = True
    for page in range(1, _MAX_HISTORY_PAGES + 1):
        batch, err = _api(f"projects/:id/jobs?per_page=100&page={page}")
        if err or not batch:
            truncated = False
            break
        collected.extend(batch)
        oldest = _age_seconds(batch[-1].get("created_at"))
        # Reach past the window by the long-job allowance: a job that finished
        # a minute ago may have been created hours before it.
        if oldest is not None and oldest > window_seconds + _LONG_JOB_ALLOWANCE_SECONDS:
            truncated = False
            break
    return (
        [
            job for job in collected
            if job.get("finished_at")
            and (_age_seconds(job["finished_at"]) or float("inf")) <= window_seconds
        ],
        truncated,
    )


def annotate_recent_work(runners: list[dict], finished: list[dict]) -> list[dict]:
    """Record per-runner completed-job counts on the runner records, in place."""
    done: dict[int, int] = {}
    for job in finished:
        rid = (job.get("runner") or {}).get("id")
        if rid is not None:
            done[rid] = done.get(rid, 0) + 1
    for runner in runners:
        runner["_recent_jobs"] = done.get(runner.get("id"), 0)
    return runners


def annotate_live_jobs(runners: list[dict], running: list[dict]) -> list[dict]:
    """Mark runners the job list shows executing work as active, in place.

    Belt and braces over `job_execution_status`: the runner object and the job
    list are two independent reads, and a runner named as owning a running job
    is alive whatever its own record claims. Cheap, and it closes the window
    where the runner detail is a moment staler than the queue.
    """
    busy = {(job.get("runner") or {}).get("id") for job in running}
    for runner in runners:
        runner[_LIVE_JOBS_MARK] = True
        if runner.get("id") in busy:
            runner["job_execution_status"] = "active"
    return runners


def starved_tags(runners: list[dict], pending: list[dict]) -> dict[str, int]:
    """{tag_key: count} for pending work no responsive runner is allowed to take.

    The one signal in this module that earned its keep: it correlates two facts
    that are each meaningless alone — a runner that stopped heartbeating, and
    work queued behind it — into a condition GitLab's own API actively denies
    by reporting the runner `online: true, job_execution_status: idle`.

    Shared by the op, the watcher and radar so all three can never disagree
    about what "stuck" means.
    """
    blocked: dict[str, int] = {}
    for job in pending:
        queued = _age_seconds(job.get("created_at"))
        if queued is not None and queued < _STARVED_MIN_QUEUE_SECONDS:
            continue
        tags = job.get("tag_list") or []
        if any(_can_serve(r, tags) and _is_responsive(r) for r in runners):
            continue
        key = ",".join(sorted(tags)) or "(untagged)"
        blocked[key] = blocked.get(key, 0) + 1
    return blocked


def waiting_for(runner: dict, pending: list[dict]) -> int:
    """How many pending jobs this runner is permitted to take."""
    return sum(1 for job in pending if _can_serve(runner, job.get("tag_list") or []))


# Options this tier understands from ops.radar.radar_tiers["gl-runners"].
# Anything else is a typo or a stale key, and a silently-ignored option is how
# someone ends up believing they configured a threshold they did not.
RADAR_OPTIONS = {"window", "quiet_when_healthy"}

# A healthy fleet is silent. This is a side concern next to whatever board the
# reader came for, and a green line per run is the noise that trains someone to
# skim past the red one.
RADAR_QUIET_DEFAULT = True

# The background watcher this tier keeps alive. Scope-free, unlike an MR feed:
# there is one runner fleet behind every board, so it is not keyed by anyone's
# population. Named here rather than in radar (#528) — radar hardcoded this
# watcher while calling gl-runners a pure tier, which leaked the tier contract
# in the direction nobody was looking.
WATCH_SOURCE = "gl-runners"
WATCH_SCOPE = "fleet"


def radar_report(options: dict | None = None) -> tuple[list[str], bool]:
    """(lines, healthy) — this op's contribution to radar. Registered, not default.

    Radar is an MR reconcile tool for most of its users, and plenty of them are
    on shared runners or below Maintainer, where reading project runners is a
    403. Turning that into a standing WARNING on every run for people who never
    asked about runners would be a regression shipped as a feature, so this only
    runs when explicitly registered in ops.radar.radar_tiers.

    The fleet watcher is claimed first, before any API call, so a token that
    cannot read runners still gets the poller it would have got before #528 —
    the spawn is not conditional on the report succeeding, and making it so
    would have been a behaviour change smuggled inside a refactor.
    """
    options = options or {}
    window = options.get("window", _THROUGHPUT_WINDOW_SECONDS)
    watch = options.get("_watch")
    if watch is not None:
        watch(WATCH_SOURCE, WATCH_SCOPE)

    listed, err = _api("projects/:id/runners?per_page=100", paginate=True)
    if err and ("403" in err or "permission denied" in err.lower()):
        return ([
            "radar: gl-runners tier is registered but this token cannot read project "
            "runners (needs Maintainer). Grant access, or drop 'gl-runners' from "
            "ops.radar.radar_tiers."
        ], False)
    if err or not listed:
        return ([f"radar: WARNING — runner fleet unreadable ({err or 'no runners listed'}). "
                 f"Runner health is UNKNOWN, not green."], False)

    details = _fetch_details(listed)
    runners = [{**r, **details.get(r["id"], {})} for r in listed]

    pending, err_pending = _api("projects/:id/jobs?scope[]=pending&per_page=100", paginate=True)
    if err_pending:
        return ([f"radar: WARNING — runner queue unreadable ({err_pending}). "
                 f"{len(runners)} runners listed; starvation is UNKNOWN."], False)
    pending = pending or []

    running, err_running = _api("projects/:id/jobs?scope[]=running&per_page=100", paginate=True)
    annotate_live_jobs(runners, [] if err_running else (running or []))
    # The history scan only buys evidence about work that is waiting. With an
    # empty queue there is no starvation question, so five pages of job history
    # answer nothing — skip them and keep a registered tier cheap.
    if pending:
        annotate_recent_work(runners, fetch_recent_finished(window)[0])

    blocked = starved_tags(runners, pending)

    if not blocked and not pending:
        # The history scan was skipped just above, so these records carry no
        # throughput evidence and their liveness is not knowable from here.
        # Printing a count anyway would be #533 in miniature: a number that
        # reads as measured and was inferred from the throttled field.
        return ([f"radar: fleet ok — {len(runners)} runners, "
                 f"0 pending, none blocked"], True)

    live = [r for r in runners if _is_responsive(r)]
    if not blocked:
        return ([f"radar: fleet ok — {len(live)}/{len(runners)} runners live, "
                 f"{len(pending)} pending, none blocked"], True)

    total = sum(blocked.values())
    lines = [f"radar: FLEET — {total} pending job(s) cannot start "
             f"({len(live)}/{len(runners)} runners live)"]
    for tags, count in sorted(blocked.items(), key=lambda kv: -kv[1]):
        owners = [r for r in runners
                  if _can_serve(r, [] if tags == "(untagged)" else tags.split(","))]
        who = ", ".join(
            f"{r.get('description')} (seen {_human_age(_age_seconds(r.get('contacted_at')))} ago)"
            for r in owners) or "NO runner carries these tags"
        lines.append(f"  [{tags}] {count} job(s) -> {who}")
    lines.append("  Pinned to an exclusive tag: no other runner may take them. "
                 "A red board in this run may be this, not your code.")
    return (lines, False)


def done_zero_unreadable(runners: list[dict],
                         running_by_runner: dict[int, int]) -> list[str]:
    """Rows whose `DONE 0` an operator would act on but the op cannot support.

    `DONE/30m 0` means "wedged" only for a runner that has been up the whole
    window. GitLab publishes no uptime and no first heartbeat to check that
    against — `created_at` is registration, `contacted_at` is last seen, and
    `runner_managers[].createdAt` (GraphQL only; absent from REST on 18.11.7)
    is the manager's first registration. All three are years old on a live
    fleet. So a host that rebooted inside the window produces the same reading
    as one that is stuck, and #531 records that misread happening twice in one
    session, in opposite directions.

    The issue's preferred fix — relabel the counter `DONE/22m` by scoping it to
    `min(window, uptime)` — is not buildable on top of that, and inferring the
    bound from job history fails in the one direction that matters: a runner up
    for hours and wedged has no activity to infer from, so it would carry the
    shortest label on the board and its `0` would read as "we only just started
    looking". That silences the wedge. Hence a stated confound rather than an
    invented number.

    Restricted to rows a reader would act on, because a caveat printed against
    every quiet runner on an idle fleet is wallpaper, and wallpaper is not
    read. Two exclusions do that work. A runner with completed jobs has
    answered the question outright. A runner GitLab itself calls `paused`,
    `offline`, `stale` or `never_contacted` has a `0` its own STATUS column
    already explains, and uptime is not the thing to go and check. What is
    left is the shape the op exists for: a runner GitLab is still advertising
    as healthy that has finished nothing — either executing work it is not
    completing, or quiet past the heartbeat threshold.
    """
    named = []
    for runner in runners:
        if runner.get("_recent_jobs"):
            continue
        if runner.get("paused") or not runner.get("active", True):
            continue
        if runner.get("status") in {"offline", "stale", "never_contacted"}:
            continue
        if running_by_runner.get(runner["id"], 0) > 0 or not _is_responsive(runner):
            named.append(runner.get("description") or f"#{runner['id']}")
    return named


def _runner_type(runner: dict) -> str:
    raw = runner.get("runner_type") or ""
    return {"instance_type": "shared", "group_type": "group", "project_type": "project"}.get(raw, raw or "?")


def _print_fleet(runners: list[dict], pending: list[dict], running: list[dict]) -> None:
    """The fleet table — one row per runner, sorted worst-health first."""
    running_by_runner: dict[int, int] = {}
    for job in running:
        rid = (job.get("runner") or {}).get("id")
        if rid is not None:
            running_by_runner[rid] = running_by_runner.get(rid, 0) + 1

    # A pending job is attributed to every runner permitted to take it, since
    # any one of them could be the one that unblocks it.
    waiting_by_runner: dict[int, int] = {}
    for job in pending:
        for runner in runners:
            if _can_serve(runner, job.get("tag_list") or []):
                waiting_by_runner[runner["id"]] = waiting_by_runner.get(runner["id"], 0) + 1

    window_minutes = _THROUGHPUT_WINDOW_SECONDS // 60
    print(f"{'ID':<5} {'DESCRIPTION':<22} {'TYPE':<8} {'STATUS':<9} {'JOB':<7} {'SEEN':<7} "
          f"{'RUN':<4} {f'DONE/{window_minutes}m':<9} {'WAIT':<5} TAGS")
    print("-" * 118)

    def sort_key(runner: dict) -> tuple:
        return (_is_responsive(runner), -(waiting_by_runner.get(runner["id"], 0)), runner["id"])

    for runner in sorted(runners, key=sort_key):
        rid = runner["id"]
        age = _age_seconds(runner.get("contacted_at"))
        responsive = _is_responsive(runner)
        waiting = waiting_by_runner.get(rid, 0)

        marker = ""
        if not responsive and waiting:
            marker = "  <! STARVED"
        elif not responsive:
            marker = "  <! silent"

        status = runner.get("status", "?")
        if runner.get("paused"):
            status = "paused"

        tags = ",".join(runner.get("tag_list") or []) or ("untagged-ok" if runner.get("run_untagged") else "-")
        print(
            f"{rid:<5} {(runner.get('description') or '?')[:22]:<22} {_runner_type(runner):<8} "
            f"{status:<9} {(runner.get('job_execution_status') or '-'):<7} {_human_age(age):<7} "
            f"{running_by_runner.get(rid, 0):<4} {runner.get('_recent_jobs', 0):<9} "
            f"{waiting:<5} {tags[:34]}{marker}"
        )

    unreadable = done_zero_unreadable(runners, running_by_runner)
    if unreadable:
        print(f"\nNOTE: DONE/{window_minutes}m 0 reads as a wedge only for a runner "
              f"that has been up the whole {window_minutes}m. GitLab publishes no "
              f"runner uptime (created_at is registration, contacted_at is last "
              f"seen), so a host that rebooted inside the window looks identical "
              f"here. Check host uptime before calling a wedge: "
              f"{', '.join(unreadable)}")


def _print_diagnosis(runners: list[dict], pending: list[dict]) -> None:
    """Name the blocked work: pending jobs no responsive runner can take."""
    blocked = starved_tags(runners, pending)

    if not blocked:
        if pending:
            print(f"\nQueue: {len(pending)} pending, all have a responsive runner. Waiting on capacity, not routing.")
        return

    total = sum(blocked.values())
    print(f"\n## STARVED — {total} pending job(s) no live runner can take")
    for tags, count in sorted(blocked.items(), key=lambda kv: -kv[1]):
        owners = [r for r in runners if _can_serve(r, tags.split(",") if tags != "(untagged)" else [])]
        if owners:
            names = ", ".join(
                f"{r.get('description')} (seen {_human_age(_age_seconds(r.get('contacted_at')))} ago)"
                for r in owners
            )
            print(f"  - {count} job(s) tagged [{tags}] -> only {names}")
        else:
            print(f"  - {count} job(s) tagged [{tags}] -> NO runner carries these tags at all")
    print("\n  Jobs pinned to an exclusive tag cannot fall back to another runner.")
    print("  Fix the runner host, or change the tag in .gitlab-ci.yml.")


def _print_queue(runners: list[dict], pending: list[dict], running: list[dict]) -> None:
    """Queue-focused view: what is waiting and what is executing, by tag."""
    print(f"## Running ({len(running)})")
    for job in sorted(running, key=lambda j: j.get("name", "")):
        runner = (job.get("runner") or {}).get("description", "-")
        print(f"  {job.get('name', '?'):<44} {job.get('ref', '?'):<24} on {runner}")

    print(f"\n## Pending ({len(pending)})")
    by_tags: dict[str, list[dict]] = {}
    for job in pending:
        by_tags.setdefault(",".join(sorted(job.get("tag_list") or [])) or "(untagged)", []).append(job)
    for tags, jobs in sorted(by_tags.items(), key=lambda kv: -len(kv[1])):
        live = [r for r in runners if _can_serve(r, tags.split(",") if tags != "(untagged)" else []) and _is_responsive(r)]
        verdict = f"{len(live)} live runner(s)" if live else "NO live runner  <!"
        print(f"  [{tags}] {len(jobs)} job(s) -> {verdict}")
        for job in jobs[:5]:
            print(f"      {job.get('name', '?'):<40} {job.get('ref', '?')}")
        if len(jobs) > 5:
            print(f"      ... and {len(jobs) - 5} more")


def _print_full(runners: list[dict]) -> None:
    """Extra per-runner detail that does not fit the table."""
    print("\n## Detail")
    for runner in sorted(runners, key=lambda r: r["id"]):
        print(f"\n  #{runner['id']} {runner.get('description') or '?'}")
        print(f"    type        : {_runner_type(runner)}  (locked={runner.get('locked')}, paused={runner.get('paused')})")
        print(f"    tags        : {', '.join(runner.get('tag_list') or []) or '-'}")
        print(f"    run_untagged: {runner.get('run_untagged')}")
        print(f"    contacted   : {runner.get('contacted_at') or '-'}")
        timeout = runner.get("maximum_timeout")
        print(f"    max timeout : {f'{timeout}s' if timeout else 'project default'}")
        projects = runner.get("projects") or []
        if projects:
            names = ", ".join(p.get("path_with_namespace", "?") for p in projects[:6])
            more = f" (+{len(projects) - 6})" if len(projects) > 6 else ""
            print(f"    projects    : {names}{more}")
        version = runner.get("version")
        if version:
            print(f"    version     : {version}  {runner.get('platform') or ''} {runner.get('architecture') or ''}".rstrip())


def main() -> int:
    mode = sys.argv[1].lower() if len(sys.argv) > 1 and sys.argv[1] else ""
    if mode and mode not in _MODES:
        print(f"ERROR: unknown mode {mode!r} — use 'full', 'queue', or omit for the fleet table")
        return 1

    listed, err = _api("projects/:id/runners?per_page=100", paginate=True)
    if err:
        print(err)
        return 1
    if not listed:
        print("No runners available to this project.")
        return 0

    details = _fetch_details(listed)
    runners = [{**runner, **details.get(runner["id"], {})} for runner in listed]

    pending, err_pending = _api("projects/:id/jobs?scope[]=pending&per_page=100", paginate=True)
    running, err_running = _api("projects/:id/jobs?scope[]=running&per_page=100", paginate=True)
    # A queue read failing is not fatal — the fleet table is still worth printing.
    queue_warning = err_pending or err_running
    pending = pending or []
    running = running or []

    annotate_live_jobs(runners, running)
    finished, throughput_truncated = fetch_recent_finished()
    annotate_recent_work(runners, finished)

    if mode == "queue":
        _print_queue(runners, pending, running)
    else:
        _print_fleet(runners, pending, running)
        _print_diagnosis(runners, pending)
        if mode == "full":
            _print_full(runners)

    if throughput_truncated:
        print(f"\nNOTE: job history hit the {_MAX_HISTORY_PAGES}-page scan cap — "
              f"DONE counts are a lower bound, not a total.")
    if queue_warning:
        print(f"\nNOTE: queue data unavailable — {queue_warning}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
