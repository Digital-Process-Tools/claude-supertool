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
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _untrusted  # noqa: E402  (a runner description and a CI tag are remote text, and these are hand-padded tables — #970)

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

# Every age in this module is GitLab's clock subtracted from ours, and nothing
# measures the gap between the two. When ours runs behind, ages come out
# NEGATIVE, and every threshold here is a `<`/`<=` a negative satisfies — so a
# heartbeat two hours in the future passed as *fresh*, a job history dated in
# the future counted as work finished just now, and the queue-age floor dropped
# every pending job as "queued too recently". That is a false ALIVE over an
# empty-looking board, on every runner at once, because skew belongs to the
# host and not to any runner: a silent all-clear over a genuinely wedged fleet,
# which `docs/validators.md` ranks above a false alarm as the more expensive
# trade. All four sites are guarded — `_is_responsive`'s heartbeat rung,
# `fetch_recent_finished`'s window, and the floor in both `classify_queue` and
# `stranded_split_for`, which are one question asked twice.
#
# A tolerance rather than a sign test, and the tolerance is the load-bearing
# half. Two NTP-synced hosts disagree by milliseconds, and a heartbeat written
# a few seconds ahead of us is the freshest reading there is; refusing every
# negative age would withdraw the evidence from an entire healthy fleet on
# ordinary jitter, which is the #750 failure rebuilt from the other side. Only
# a gap too large to be jitter means the comparison is unusable.
_CLOCK_SKEW_TOLERANCE_SECONDS = 60

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


def _usable_age(seconds: float | None) -> bool:
    """Is this age a measurement, or a statement about our own clock?

    Separated from the comparisons it guards because it has to be applied at
    every one of them, and there are four. Guarding only the rung that produced
    the report leaves the others forging the same false answer: the first cut of
    this fix covered `_is_responsive` and `fetch_recent_finished` and left the
    queue-age floor, so a skewed fleet still rendered an empty board — the
    liveness verdict was honest and the queue it was about had vanished.
    """
    return seconds is not None and seconds >= -_CLOCK_SKEW_TOLERANCE_SECONDS


def _human_age(seconds: float | None) -> str:
    """Compact age: 45s, 12m, 3h, 5d — or the reason there is no age to give."""
    if seconds is None:
        return "-"
    if not _usable_age(seconds):
        # Printing `-3600s` renders a runner as seen minus-one-hour ago, which
        # reads as a very odd measurement rather than as the absence of one.
        # This row is the only place an operator can learn their clock is wrong.
        return "ahead of our clock"
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


# GitLab's own verdicts that a runner is not going to pick anything up. These
# are answers, not absences: the instance has decided, and nothing here is
# inferring it from a field whose write policy we do not control.
_GITLAB_DOWN_STATUSES = {"offline", "stale", "never_contacted"}


def _demonstrably_down(runner: dict) -> bool:
    """GitLab itself says this runner cannot take work right now.

    The mirror image of `_is_responsive`, and deliberately not its negation.
    Liveness needs positive evidence; so does death. What sits between the two
    is the shape that produced #750 — a runner GitLab still advertises as
    `online`, not paused, whose only demerit is `contacted_at` age. That field
    is throttled (see `_HEARTBEAT_WARN_SECONDS`), so its age is a reason to go
    and look, never a finding on its own. Records in that gap are UNKNOWN, and
    the callers say so rather than picking whichever of the two answers is
    convenient.
    """
    if runner.get("paused") or not runner.get("active", True):
        return True
    return runner.get("status") in _GITLAB_DOWN_STATUSES


def status_phrase(runner: dict) -> str:
    """What GitLab says about this runner, and what to do about it.

    Written from the record rather than hardcoded: the watcher used to append
    "GitLab still reports it online" to every silence event, which was false
    for the two states that most often produce one. A reader told a `stale`
    438-day registration is "still online" goes hunting for a wedged host
    instead of deleting a record nobody deregistered.
    """
    if runner.get("paused"):
        return "GitLab reports it paused — un-pause it, or move the tag to a live runner"
    if not runner.get("active", True):
        return "GitLab reports it inactive"
    status = runner.get("status")
    if status in _GITLAB_DOWN_STATUSES:
        return f"GitLab reports it {status} — the registration may simply need deleting"
    return "GitLab still reports it online"


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


def _liveness_unknown(runner: dict) -> bool:
    """Neither predicate above will speak for this runner.

    #805 gave the module three states and only two predicates: `_is_responsive`
    for evidence of life, `_demonstrably_down` for evidence of death, and the
    gap between them left to whichever caller happened to negate one of them.
    A negation is not the third state — `not _is_responsive` collapses UNKNOWN
    into "down" and `not _demonstrably_down` collapses it into "live", and both
    readings have shipped a false alarm from this file already.

    So the gap gets a name. A runner GitLab still advertises as `online` and
    un-paused, whose only demerit is `contacted_at` age, is not silent and not
    fine: it is unmeasured. Callers that must act on the distinction ask for it
    by name rather than inverting a predicate that was built to answer a
    different question.
    """
    return not _demonstrably_down(runner) and not _is_responsive(runner)


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
    # `_usable_age` before the ceiling, not folded into it: an age we cannot
    # interpret is not a fresh one, and `age <= ceiling` says yes to both.
    return _usable_age(age) and age <= _HEARTBEAT_WARN_SECONDS


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
            and _usable_age(_age_seconds(job["finished_at"]))
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


def annotate_live_jobs(runners: list[dict],
                       running: list[dict] | None) -> list[dict]:
    """Mark runners the job list shows executing work as active, in place.

    Belt and braces over `job_execution_status`: the runner object and the job
    list are two independent reads, and a runner named as owning a running job
    is alive whatever its own record claims. Cheap, and it closes the window
    where the runner detail is a moment staler than the queue.

    `running=None` means the list was not read — an API error, a timeout — and
    is deliberately not `[]`. `[]` is an observation: nobody is executing. None
    is the absence of one, and the two produce opposite verdicts about a busy
    runner. So None leaves the annotation mark unset and every later liveness
    question about these records raises `UnannotatedFleetError` rather than
    falling through onto `contacted_at`, the throttled field. A caller that
    coerces its own error to `[]` here re-creates #1112: `hercule` was reported
    `runner_liveness_unknown` with `running_on_it="0"` while executing two jobs,
    because one failed read answered both the count and the verdict.
    """
    if running is None:
        return runners
    busy = {(job.get("runner") or {}).get("id") for job in running}
    for runner in runners:
        runner[_LIVE_JOBS_MARK] = True
        if runner.get("id") in busy:
            runner["job_execution_status"] = "active"
    return runners


def classify_queue(
    runners: list[dict], pending: list[dict]
) -> tuple[dict[str, int], dict[str, int]]:
    """({tag_key: count} stuck, {tag_key: count} unproven) for waiting work.

    The one signal in this module that earned its keep — it correlates two
    facts that are each meaningless alone, a runner that stopped heartbeating
    and work queued behind it, into a condition GitLab's own API actively
    denies by reporting the runner `online: true, job_execution_status: idle`.
    It also over-claimed, because the correlation has three outcomes and it
    published two ([#750](https://github.com/Digital-Process-Tools/claude-supertool/issues/750)).

    A pending job past the queue-age floor that no responsive runner may take
    lands in one of two buckets, decided by *why* its candidates are not
    responsive:

    **stuck** — every runner that may take it is demonstrably down: paused,
    inactive, or `offline`/`stale`/`never_contacted` by GitLab's own reckoning;
    or no runner in the fleet carries its tags at all. There is no uncertainty
    in either of those and the finding is stated flatly.

    **unproven** — at least one candidate is still advertised `online` and not
    paused, and failed `_is_responsive` only on `contacted_at` age. That field
    is throttled, which is why it is consulted last and at 30 minutes; a fleet
    idling behind one long job routinely reads minutes stale across every row
    at once. Calling that starvation is the fleet-wide false alarm this module
    already learned once, arriving through the queue instead of the runner.

    Unproven is disclosed, never dropped. Silence here would trade a loud false
    alarm for a quiet false all-clear, and the queue is genuinely waiting
    either way — see `docs/validators.md` §"Declining instead of guessing".

    Shared by the op, the watcher and radar so all three can never disagree
    about what "stuck" means, or about which of the three answers they have.
    """
    stuck: dict[str, int] = {}
    unproven: dict[str, int] = {}
    for job in pending:
        queued = _age_seconds(job.get("created_at"))
        # `_usable_age` guards the floor for the same reason it guards the two
        # rungs in `_is_responsive`: the floor exists to dismiss work that has
        # not waited long enough to be stuck, and a negative age is not evidence
        # the job is fresh — it is our clock disagreeing with GitLab's. Skew is
        # host-wide, so `created_at` goes ahead exactly when `contacted_at`
        # does, and an unguarded floor empties the whole board at the moment the
        # heartbeat evidence disappears too. Unusable means classify it.
        if _usable_age(queued) and queued < _STARVED_MIN_QUEUE_SECONDS:
            continue
        tags = job.get("tag_list") or []
        candidates = [r for r in runners if _can_serve(r, tags)]
        if any(_is_responsive(r) for r in candidates):
            continue
        key = ",".join(sorted(tags)) or "(untagged)"
        bucket = stuck if all(_demonstrably_down(r) for r in candidates) else unproven
        bucket[key] = bucket.get(key, 0) + 1
    return stuck, unproven


def starved_tags(runners: list[dict], pending: list[dict]) -> dict[str, int]:
    """{tag_key: count} for work that is stuck — the stated half of the verdict.

    Kept as the name for the finding alone. Anything wanting the whole picture
    has to ask for both buckets and decide what to do with the second, which is
    the point: a caller cannot reach the unproven queue by accident and cannot
    silently discard it either.
    """
    return classify_queue(runners, pending)[0]


def waiting_for(runner: dict, pending: list[dict]) -> int:
    """How many pending jobs this runner is permitted to take."""
    return sum(1 for job in pending if _can_serve(runner, job.get("tag_list") or []))


def stranded_for(runner: dict, pending: list[dict], fleet: list[dict]) -> int:
    """Pending jobs this runner may take that no responsive runner in `fleet` may.

    What a runner is *permitted* to take and what is *stuck behind it* are two
    different questions, and `waiting_for` only answers the first. A runner
    decommissioned in the fleet but left in GitLab beside a same-tag successor
    is permitted to take every job the successor is happily running — forever.
    Silence gated on `waiting_for` therefore alarms on that pair on every poll
    until somebody deletes the record, and an alarm that is always noise
    teaches its reader to skim the board, which is how the real starvation
    underneath it gets missed.

    Coverage is judged per job, never per tag, because GitLab routes on "one
    runner carries *all* of a job's tags". Asking instead whether the silent
    runner's own tags are covered is wrong in both directions: a tag no queued
    job asks for would alarm forever, and two live runners splitting `docker`
    and `gpu` between them would read as coverage for a job needing both that
    can start on neither.

    Suppression requires positive evidence — a runner that passes
    `_is_responsive` and may take the job. An unannotated fleet raises rather
    than answering, on the same terms as `_is_responsive` itself: a coverage
    check resting on the throttled field would silence real wedges.
    """
    return sum(stranded_split_for(runner, pending, fleet))


def stranded_split_for(
    runner: dict, pending: list[dict], fleet: list[dict]
) -> tuple[int, int]:
    """(stuck, unproven) counts of the work stranded behind one runner.

    `classify_queue` asked per tag-key across the whole queue; this asks the
    same question per runner, so the fleet table can mark a row from the very
    computation that writes the footer under it. They disagreed before #750 —
    the row matched pending tags against any non-heartbeating runner and
    skipped the queue-age floor entirely, so a render could carry `<! STARVED`
    rows above "all have a responsive runner. Waiting on capacity, not
    routing." Both were printed from the same data and only one was right;
    a reader acting on either reached the opposite conclusion to the other.

    The split follows `classify_queue` exactly: work whose every candidate is
    demonstrably down is stuck, work with a candidate GitLab still calls online
    is unproven.
    """
    live = [r for r in fleet if _is_responsive(r)]
    stuck = 0
    unproven = 0
    for job in pending:
        queued = _age_seconds(job.get("created_at"))
        # Same floor, same guard — see `classify_queue`. The row and the footer
        # are one question asked twice and the docs promise they cannot
        # disagree; guarding only one of them is how they start to.
        if _usable_age(queued) and queued < _STARVED_MIN_QUEUE_SECONDS:
            continue
        tags = job.get("tag_list") or []
        if not _can_serve(runner, tags):
            continue
        if any(_can_serve(candidate, tags) for candidate in live):
            continue
        candidates = [r for r in fleet if _can_serve(r, tags)]
        if all(_demonstrably_down(r) for r in candidates):
            stuck += 1
        else:
            unproven += 1
    return stuck, unproven


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
    if err_running:
        # Same refusal as the pending read above, and for a stronger reason: a
        # job executing is the best liveness proof this module has, so losing
        # the list drops every verdict below onto the throttled contacted_at.
        # Coercing the error to `[]` is what shipped #1112.
        return ([f"radar: WARNING — the running-jobs list is unreadable "
                 f"({err_running}). It is the strongest liveness evidence there "
                 f"is, so runner health is UNKNOWN, not green."], False)
    annotate_live_jobs(runners, running or [])
    # The history scan only buys evidence about work that is waiting. With an
    # empty queue there is no starvation question, so five pages of job history
    # answer nothing — skip them and keep a registered tier cheap.
    if pending:
        annotate_recent_work(runners, fetch_recent_finished(window)[0])

    blocked, unproven = classify_queue(runners, pending)

    if not blocked and not unproven and not pending:
        # The history scan was skipped just above, so these records carry no
        # throughput evidence and their liveness is not knowable from here.
        # Printing a count anyway would be #533 in miniature: a number that
        # reads as measured and was inferred from the throttled field.
        #
        # This return is load-bearing, not cosmetic (#807). The `_is_responsive`
        # count below raises on an unannotated record, and the only thing that
        # keeps it from being asked is that it sits under this branch. Hoisting
        # it, merging the two arms or adding an early return below it crashes a
        # live radar run; `tests/test_radar_runners_unannotated_count_807.py`
        # is what says so, since the invariant is invisible from either site.
        return ([f"radar: fleet ok — {len(runners)} runners, "
                 f"0 pending, none blocked"], True)

    live = [r for r in runners if _is_responsive(r)]
    if not blocked and not unproven:
        return ([f"radar: fleet ok — {len(live)}/{len(runners)} runners live, "
                 f"{len(pending)} pending, none blocked"], True)

    lines: list[str] = []
    if blocked:
        total = sum(blocked.values())
        lines.append(f"radar: FLEET — {total} pending job(s) cannot start "
                     f"({len(live)}/{len(runners)} runners live)")
        for tags, count in sorted(blocked.items(), key=lambda kv: -kv[1]):
            who = _owner_names(runners, tags) or "NO runner carries these tags"
            lines.append(f"  [{_untrusted.flat(tags)}] {count} job(s) -> {who}")
        lines.append("  Pinned to an exclusive tag: no other runner may take them. "
                     "A red board in this run may be this, not your code.")
    if unproven:
        # Not green. An unproven queue is the tier declining, and a decline
        # rendered as `fleet ok` is the false all-clear one layer up.
        total = sum(unproven.values())
        lines.append(f"radar: FLEET UNKNOWN — {total} pending job(s) waiting on runners "
                     f"whose liveness could not be established")
        for tags, count in sorted(unproven.items(), key=lambda kv: -kv[1]):
            lines.append(f"  [{_untrusted.flat(tags)}] {count} job(s) "
                         f"-> {_owner_names(runners, tags)}")
        lines.append("  GitLab reports them online; only contacted_at age says "
                     "otherwise, and GitLab throttles it. Check the hosts.")
    return (lines, False)


def done_zero_unreadable(runners: list[dict],
                         running_by_runner: dict[int, int],
                         waiting_by_runner: dict[int, int]) -> list[str]:
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
    read. Three gates do that work.

    A runner with completed jobs has answered the question outright. A runner
    GitLab itself calls `paused`, `offline`, `stale` or `never_contacted` has a
    `0` its own STATUS column already explains, and uptime is not the thing to
    go and check there — which is why `_demonstrably_down` excludes rather than
    selects here, though #806 raised the opposite as a candidate.

    The third gate is stake, and it is the one #806 was actually about. The
    membership test used to be `not _is_responsive`, which after #805 reads a
    runner in the UNKNOWN gap — advertised `online`, un-paused, stale only on
    the throttled heartbeat — as though it were down. Live, that named
    `docker-db-on-disk`: `RUN 0`, `WAIT 0`, holding no job and blocking no
    queued work, sent to a reader as a host to go and check before calling a
    wedge nobody had called. Worse, an idle fleet is exactly when every
    heartbeat drifts stale at once, so the exclusions written to prevent
    wallpaper produced it on the one fleet shape they were aimed at: the only
    quiet runner they excluded was one with a *fresh* heartbeat, and a fresh
    heartbeat is the first thing an idle fleet loses.

    So the `0` is caveated where a wedge reading has something to be wedged on:
    a runner holding running jobs and completing none, or one whose liveness is
    UNKNOWN with pending work queued that it may take. With nothing at stake
    there is no wedge to disclaim, the row's own marker already says the
    heartbeat is stale, and the note stays quiet.
    """
    named = []
    for runner in runners:
        if runner.get("_recent_jobs"):
            continue
        if _demonstrably_down(runner):
            continue
        holding = running_by_runner.get(runner["id"], 0) > 0
        blocking = (waiting_by_runner.get(runner["id"], 0) > 0
                    and _liveness_unknown(runner))
        if holding or blocking:
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

        # Written from `stranded_split_for`, which is what `_print_diagnosis`
        # totals two lines further down. Marking the row from `waiting` — any
        # pending job this runner is permitted to take — put `<! STARVED` above
        # a footer reading "all have a responsive runner" in the same render.
        # `stuck`/`unproven` count stranded *work*, so on an empty queue both are
        # 0 and every unresponsive row used to fall through to `<! silent` —
        # including the one GitLab still advertises `online`, whose only demerit
        # is the throttled heartbeat (#814). The reader got `online` and `silent`
        # on one row with nothing saying which half to believe. So the last
        # branch is keyed on the evidence rather than on the negation: `silent`
        # only where GitLab itself says down, and the gap goes to the UNKNOWN
        # this table already prints one line above.
        stuck, unproven = stranded_split_for(runner, pending, runners)
        marker = ""
        if not responsive and stuck:
            marker = "  <! STARVED"
        elif not responsive and (unproven or _liveness_unknown(runner)):
            marker = "  <! UNKNOWN"
        elif not responsive:
            marker = "  <! silent"

        status = runner.get("status", "?")
        if runner.get("paused"):
            status = "paused"

        tags = ",".join(runner.get("tag_list") or []) or ("untagged-ok" if runner.get("run_untagged") else "-")
        # Flatten *before* slicing, not after. `flat()` can widen a field — a
        # stream that cannot carry U+2028 spells it `[U+2028]`, eight characters
        # for one (#863) — so flattening a cell already cut to its column width
        # trades a row that splits for a row that overflows into the next
        # column. Truncation last is what keeps `:<22` a width and not a floor.
        description = _untrusted.flat(runner.get("description") or "?")
        tags = _untrusted.flat(tags)
        print(
            f"{rid:<5} {description[:22]:<22} {_runner_type(runner):<8} "
            f"{status:<9} {(runner.get('job_execution_status') or '-'):<7} {_human_age(age):<7} "
            f"{running_by_runner.get(rid, 0):<4} {runner.get('_recent_jobs', 0):<9} "
            f"{waiting:<5} {tags[:34]}{marker}"
        )

    unreadable = done_zero_unreadable(runners, running_by_runner, waiting_by_runner)
    if unreadable:
        print(f"\nNOTE: DONE/{window_minutes}m 0 reads as a wedge only for a runner "
              f"that has been up the whole {window_minutes}m. GitLab publishes no "
              f"runner uptime (created_at is registration, contacted_at is last "
              f"seen), so a host that rebooted inside the window looks identical "
              f"here. Check host uptime before calling a wedge: "
              f"{', '.join(_untrusted.flat(name) for name in unreadable)}")


def _owner_names(runners: list[dict], tags: str) -> str:
    """The runners permitted to take work carrying `tags`, with heartbeat ages."""
    owners = [r for r in runners
              if _can_serve(r, tags.split(",") if tags != "(untagged)" else [])]
    return ", ".join(
        f"{_untrusted.flat(str(r.get('description')))} "
        f"(seen {_human_age(_age_seconds(r.get('contacted_at')))} ago)"
        for r in owners
    )


def _print_diagnosis(runners: list[dict], pending: list[dict]) -> None:
    """Name the waiting work, in the three states it can be in.

    `ok`, a finding, and a decline. The middle one is work whose candidates
    GitLab itself calls down; the last is work whose candidates GitLab still
    advertises as online and whose only demerit is the throttled heartbeat.
    Printing the second as the first is #750; printing it as the all-clear
    sentence would be the more expensive mistake in the other direction.
    """
    blocked, unproven = classify_queue(runners, pending)

    if not blocked and not unproven:
        # Only jobs past the queue-age floor were routed at all, so the
        # all-clear may only speak for those. A queue entirely below the floor
        # sitting behind an unresponsive runner used to print "all have a
        # responsive runner", which is the false all-clear this whole section
        # is about, reached from the other end.
        judged = [job for job in pending
                  if (_age_seconds(job.get("created_at")) or float("inf"))
                  >= _STARVED_MIN_QUEUE_SECONDS]
        if judged:
            print(f"\nQueue: {len(pending)} pending, all have a responsive runner. Waiting on capacity, not routing.")
        elif pending:
            floor = _STARVED_MIN_QUEUE_SECONDS // 60
            print(f"\nQueue: {len(pending)} pending, none waiting longer than "
                  f"{floor}m — too soon to call it routing or capacity.")
        return

    if blocked:
        total = sum(blocked.values())
        print(f"\n## STARVED — {total} pending job(s) no live runner can take")
        for tags, count in sorted(blocked.items(), key=lambda kv: -kv[1]):
            names = _owner_names(runners, tags)
            shown = _untrusted.flat(tags)
            if names:
                print(f"  - {count} job(s) tagged [{shown}] -> only {names}")
            else:
                print(f"  - {count} job(s) tagged [{shown}] -> NO runner carries these tags at all")
        print("\n  Jobs pinned to an exclusive tag cannot fall back to another runner.")
        print("  Fix the runner host, or change the tag in .gitlab-ci.yml.")

    if unproven:
        total = sum(unproven.values())
        print(f"\n## UNKNOWN — {total} pending job(s) whose routing could not be established")
        for tags, count in sorted(unproven.items(), key=lambda kv: -kv[1]):
            print(f"  - {count} job(s) tagged [{_untrusted.flat(tags)}] "
                  f"-> {_owner_names(runners, tags)}")
        print("\n  GitLab still advertises these runners as online and un-paused; they")
        print("  fail the liveness check only on contacted_at age, which GitLab")
        print("  throttles — a fleet idling behind one long job reads stale on every")
        print("  row at once. Not shown stuck, and not shown fine. Check the hosts.")


def _print_queue(runners: list[dict], pending: list[dict], running: list[dict]) -> None:
    """Queue-focused view: what is waiting and what is executing, by tag."""
    print(f"## Running ({len(running)})")
    for job in sorted(running, key=lambda j: j.get("name", "")):
        runner = _untrusted.flat(str((job.get("runner") or {}).get("description", "-")))
        name = _untrusted.flat(str(job.get("name", "?")))
        ref = _untrusted.flat(str(job.get("ref", "?")))
        print(f"  {name:<44} {ref:<24} on {runner}")

    print(f"\n## Pending ({len(pending)})")
    by_tags: dict[str, list[dict]] = {}
    for job in pending:
        by_tags.setdefault(",".join(sorted(job.get("tag_list") or [])) or "(untagged)", []).append(job)
    for tags, jobs in sorted(by_tags.items(), key=lambda kv: -len(kv[1])):
        live = [r for r in runners if _can_serve(r, tags.split(",") if tags != "(untagged)" else []) and _is_responsive(r)]
        verdict = f"{len(live)} live runner(s)" if live else "NO live runner  <!"
        print(f"  [{_untrusted.flat(tags)}] {len(jobs)} job(s) -> {verdict}")
        for job in jobs[:5]:
            name = _untrusted.flat(str(job.get("name", "?")))
            print(f"      {name:<40} {_untrusted.flat(str(job.get('ref', '?')))}")
        if len(jobs) > 5:
            print(f"      ... and {len(jobs) - 5} more")


def _print_full(runners: list[dict]) -> None:
    """Extra per-runner detail that does not fit the table."""
    print("\n## Detail")
    for runner in sorted(runners, key=lambda r: r["id"]):
        print(f"\n  #{runner['id']} "
              f"{_untrusted.flat(runner.get('description') or '?')}")
        print(f"    type        : {_runner_type(runner)}  (locked={runner.get('locked')}, paused={runner.get('paused')})")
        print(f"    tags        : "
              f"{_untrusted.flat(', '.join(runner.get('tag_list') or []) or '-')}")
        print(f"    run_untagged: {runner.get('run_untagged')}")
        print(f"    contacted   : {runner.get('contacted_at') or '-'}")
        timeout = runner.get("maximum_timeout")
        print(f"    max timeout : {f'{timeout}s' if timeout else 'project default'}")
        projects = runner.get("projects") or []
        if projects:
            names = ", ".join(_untrusted.flat(str(p.get("path_with_namespace", "?")))
                              for p in projects[:6])
            more = f" (+{len(projects) - 6})" if len(projects) > 6 else ""
            print(f"    projects    : {names}{more}")
        version = runner.get("version")
        if version:
            # Version, platform and architecture are self-reported by the runner
            # binary at registration, so they are the host's text and not
            # GitLab's — same trust as the description two lines up.
            print(f"    version     : {_untrusted.flat(str(version))}  "
                  f"{_untrusted.flat(str(runner.get('platform') or ''))} "
                  f"{_untrusted.flat(str(runner.get('architecture') or ''))}".rstrip())


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
    if err_running:
        # Not the same call as the pending one below it. Every liveness marker
        # in the table — and the whole of `:queue`'s Running section — is
        # derived from this list, so printing without it states verdicts built
        # on the throttled contacted_at alone and renders busy runners as idle
        # (#1112). Refusing is the honest outcome; the note is not enough.
        print(f"ERROR: the running-jobs list is unreadable — {err_running}. "
              f"Every liveness verdict in this view is derived from it. Printing "
              f"the fleet without it would report runners executing jobs as "
              f"unaccounted for.")
        return 1
    # A pending read failing is not fatal — the fleet table is still worth printing.
    queue_warning = err_pending
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
