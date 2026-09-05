"""gl-runners watcher source — continuous health watch over the runner fleet.

The failure this exists for: a runner stops polling GitLab, and GitLab keeps
reporting it `status: online, job_execution_status: idle` for the best part of
an hour, because idle and dead are the same two fields. Jobs pinned to that
runner's exclusive tag queue up behind it and cannot fall back to any other
runner. Nothing turns red. You find out when someone asks why the pipeline is
slow today.

Like `gitlab-mr-feed`, the id here is a *scope*, not a member — the interesting
events are about runners you are not watching yet, including ones added after
the poller started. Never terminal: a fleet has no end state.

    state    {runners: {id: snapshot}, starved: {tagkey: count}}
    silent   not taking work AND work stranded behind it — a wedge
    starved  pending jobs whose tags match only wedged runners
    fleet    membership and paused-flag changes

`runner_silent` gates on consequence, and that is not a refinement — it is the
whole difference between a signal and noise. Built on heartbeat staleness alone
it fired on 6 of 6 online runners in its first hour, because GitLab throttles
its contacted_at writes and a runner taking jobs continuously still reads
minutes stale. A signal that fires on the entire healthy fleet does not just
fail to inform, it buries the one event that was real. So liveness is judged
first on job_execution_status and the running-jobs list — direct proof — and
silence is only reported when jobs are actually queued behind it *and no live
runner is allowed to take them*. That last clause is the same lesson a second
time: a runner decommissioned but left in GitLab beside a same-tag successor
satisfies both of the earlier halves on every single poll, forever, while the
work routes to the successor and runs fine. Consequence is per job, not per
tag — see `runners_op.stranded_for`.

Baseline handling is deliberately asymmetric on the first tick. Membership,
silence and paused flags are recorded quietly, because announcing a transition
that happened before the watcher existed is history, not news. Starvation is
announced immediately, because it is not a transition — it is a condition that
is still true right now, and staying silent about jobs that cannot move would
make a freshly-spawned watcher indistinguishable from a healthy fleet.

Source plugin contract:
- INTERVAL: int seconds between polls
- poll(state, ctx) -> (events, new_state)
- is_terminal(state) -> bool
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

# Runner state moves on infrastructure timescales, not CI timescales, and each
# tick costs one list call plus one detail call per runner plus two queue
# calls. A minute keeps the silent-runner alert inside the window where it is
# still actionable without hammering the API.
INTERVAL = 60

# GitLab attributes a job's failure to the runner itself -- disk full,
# `prepare_executor` dying -- via `failure_reason == RUNNER_SYSTEM_FAILURE`
# rather than to the pipeline's own script (see `annotate_systemic_failures`).
# The issue's own worked example -- a runner failing every job for the best
# part of an hour -- clears this comfortably, while a single flaky job inside
# the throughput window does not reach it. Counted over
# `runners_op._THROUGHPUT_WINDOW_SECONDS` (30 minutes), the window
# `_fetch_fleet` already fetches on every tick for the `_recent_jobs` count --
# no separate window or state needed for this. Tune here, not in the
# conditional below (#2296).
_SYSTEMIC_FAILURE_THRESHOLD = 3

_PRESETS_DIR = Path(__file__).parents[3]


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# One source of truth for "responsive" and "can this runner take this job".
# If the op and the watcher ever disagreed on those, the table would say
# STARVED while the watcher stayed quiet, and neither would be obviously wrong.
runners_op = _load("watch_gl_runners_op", _PRESETS_DIR / "gitlab" / "runners.py")


def _fetch_fleet() -> tuple[list[dict], list[dict] | None, list[dict]] | None:
    """(runners, pending_jobs, running_jobs) with detail merged. None on failure.

    None is deliberately not an empty list: an unreachable GitLab must never
    read as "every runner vanished", which would fire a departure event for
    each of them and a recovery event for none.

    The running list is fetched before anything is judged, because a runner
    executing a job is alive regardless of what its throttled contacted_at
    says — `annotate_live_jobs` folds that proof into the runner records.

    So a running list that could not be read fails the whole tick, and is not
    coerced to `[]` the way the pending list is coerced to None. Two things
    hang off that one call and both break together: `_is_responsive` loses its
    strongest evidence and drops onto the throttled heartbeat, and the
    `running_on_it` count that rides on every event becomes a fabricated zero.
    That is #1112 — `hercule` reported `runner_liveness_unknown` with
    `running_on_it="0"` while executing two jobs of a live MR pipeline, and
    retracted 69 seconds later with nothing about the fleet having changed.
    A tick that could not look has not observed a quiet fleet.
    """
    listed, err = runners_op._api("projects/:id/runners?per_page=100", paginate=True)
    if err or listed is None:
        return None

    details = runners_op._fetch_details(listed)
    merged = [{**runner, **details.get(runner["id"], {})} for runner in listed]

    running, err_running = runners_op._api(
        "projects/:id/jobs?scope[]=running&per_page=100", paginate=True)
    if err_running:
        return None
    running = running or []
    runners_op.annotate_live_jobs(merged, running)
    finished, _truncated = runners_op.fetch_recent_finished()
    runners_op.annotate_recent_work(merged, finished)
    # Same finished-job window, a different verdict — see
    # `annotate_systemic_failures` (#2296). No second API call: the fetch
    # above already runs on every tick for the throughput count.
    runners_op.annotate_systemic_failures(merged, finished)

    pending, err_pending = runners_op._api(
        "projects/:id/jobs?scope[]=pending&per_page=100", paginate=True
    )
    if err_pending:
        # The fleet half is still usable; treat the queue as unknown rather
        # than empty, so a queue outage cannot read as "starvation cleared".
        return merged, None, running
    return merged, (pending or []), running


def _snapshot(runner: dict, pending: list[dict], running: list[dict],
              fleet: list[dict]) -> dict[str, Any]:
    """The fields whose changes are worth an event.

    `waiting`, `stranded` and `running` ride on every snapshot rather than only
    the starved one: those counts are exactly what separates a runner that is
    idle from one that is wedged, and leaving them off forces whoever reads the
    event to go and fetch them by hand before they can act on it.

    `fleet` is the whole runner list because the interesting question about one
    runner cannot be answered from that runner alone — whether its queue is
    stuck depends on who else is up.
    """
    responsive = runners_op._is_responsive(runner)
    waiting = runners_op.waiting_for(runner, pending)
    stuck, unproven = runners_op.stranded_split_for(runner, pending, fleet)
    stranded = stuck + unproven
    return {
        "description": runner.get("description") or f"#{runner.get('id')}",
        "responsive": responsive,
        "paused": bool(runner.get("paused")),
        "tags": sorted(runner.get("tag_list") or []),
        "contacted_at": runner.get("contacted_at"),
        "status_phrase": runners_op.status_phrase(runner),
        "waiting": waiting,
        "stranded": stranded,
        "stuck": stuck,
        "unproven": unproven,
        "running": sum(1 for job in running
                       if (job.get("runner") or {}).get("id") == runner.get("id")),
        "recent_jobs": runner.get("_recent_jobs", 0),
        "systemic_failures": runner.get("_systemic_failures", 0),
        "systemically_failing": runner.get("_systemic_failures", 0)
                                >= _SYSTEMIC_FAILURE_THRESHOLD,
        # Silence is only news when work is stuck behind it. A quiet runner with
        # an empty queue is a runner with nothing to do, and alerting on that
        # fires on the whole fleet every time the pipeline goes idle. "Stuck
        # behind it" is per job and not per matching tag: jobs a live runner may
        # also take are not stuck, they are routed.
        #
        # And it is only a *wedge* when GitLab itself says the runner is down.
        # Where the only demerit is `contacted_at` age the two are separated:
        # `blocked` states a fault, `unconfirmed` declines to (#750).
        "blocked": (not responsive) and stuck > 0,
        "unconfirmed": (not responsive) and unproven > 0,
        # Quiet, tags fully covered by a live runner: a stale record somebody
        # can delete, not an incident. Recorded so the state answers "why is
        # this runner not firing" on demand, and deliberately not an event —
        # see docs/presets/watch.md.
        "superseded": (not responsive) and waiting > 0 and stranded == 0,
    }


def _fleet_events(
    previous: dict[str, dict], current: dict[str, dict], first_tick: bool,
    queue_known: bool = True,
) -> list[dict]:
    """Membership, blocked-silence and paused transitions. Quiet on first tick.

    The silence transition is on `blocked`, not on `responsive`. Heartbeat
    staleness alone fires on healthy runners — GitLab throttles its
    contacted_at writes, so a busy runner routinely reads as minutes stale —
    and a signal that fires on the whole fleet buries the one that matters.
    """
    if first_tick:
        return []

    events: list[dict] = []

    for rid, now in current.items():
        was = previous.get(rid)
        name = now["description"]

        counts = {"pending_for_it": now["waiting"], "running_on_it": now["running"],
                  "completed_recently": now.get("recent_jobs", 0)}
        # The transport carries strings, booleans and finite numbers, and
        # refuses anything else out loud — so a list reached the channel as
        # `unsendable="tags (array)"` on every single runner event, which is
        # the one field telling an operator which jobs this runner can take.
        # Joined in `classify_queue`'s key format, so a runner event and a
        # queue event name the same tag set the same way (#1112).
        tagkey = ",".join(now["tags"]) or "(untagged)"

        if was is None:
            events.append({
                "event": "runner_added",
                "payload": {"runner_id": rid, "description": name, "tags": tagkey,
                            **counts},
                "notify_title": f"runner {name} joined",
                "notify_message": ", ".join(now["tags"]) or "untagged",
            })
            continue

        if queue_known and now["blocked"] and not was.get("blocked"):
            age = runners_op._human_age(runners_op._age_seconds(now["contacted_at"]))
            events.append({
                "event": "runner_silent",
                "payload": {"runner_id": rid, "description": name, "last_seen": age,
                            "tags": tagkey,
                            "stranded_for_it": now["stuck"], **counts},
                "notify_title": f"runner {name} wedged — {now['stuck']} job(s) stuck",
                # The reason comes off the record. Appending "GitLab still
                # reports it online" unconditionally was false for the two
                # states that most often reach here, and it sends the reader to
                # audit a host when the fix is un-pausing or deleting (#750).
                "notify_message": f"last contact {age} ago, {now['running']} running "
                                  f"— {now['status_phrase']}",
            })
        elif queue_known and now["unconfirmed"] and not was.get("unconfirmed"):
            age = runners_op._human_age(runners_op._age_seconds(now["contacted_at"]))
            events.append({
                "event": "runner_liveness_unknown",
                "payload": {"runner_id": rid, "description": name, "last_seen": age,
                            "tags": tagkey,
                            "unproven_for_it": now["unproven"], **counts},
                "notify_title": f"runner {name} unaccounted for — "
                                f"{now['unproven']} job(s) waiting on it",
                "notify_message": f"last contact {age} ago and GitLab still reports it "
                                  f"online; heartbeat age is throttled, so this is not "
                                  f"a wedge — go and check the host",
            })
        elif queue_known and (was.get("blocked") or was.get("unconfirmed")) and now["responsive"]:
            events.append({
                "event": "runner_recovered",
                "payload": {"runner_id": rid, "description": name, **counts},
                "notify_title": f"runner {name} is back",
                "notify_message": f"heartbeat resumed, {now['waiting']} pending",
            })

        if now["paused"] != was["paused"]:
            events.append({
                "event": "runner_paused",
                "payload": {"runner_id": rid, "description": name, "paused": now["paused"],
                            **counts},
                "notify_title": f"runner {name} {'paused' if now['paused'] else 'unpaused'}",
                "notify_message": ", ".join(now["tags"]) or "untagged",
            })

        # Orthogonal to responsive/blocked above: a runner can be `online`,
        # heartbeating, taking jobs, and still killing every one of them
        # (disk full, `prepare_executor` dying) -- the incident this pair
        # exists for (#2296). Quiet on first tick, same as the transitions
        # above it, for the reason the module docstring gives: a state that
        # predates the watcher is history, not news.
        if now["systemically_failing"] and not was.get("systemically_failing"):
            events.append({
                "event": "runner_failing_systemically",
                "payload": {"runner_id": rid, "description": name,
                            "failed_for_it": now["systemic_failures"], **counts},
                "notify_title": f"runner {name} failing systemically — "
                                f"{now['systemic_failures']} job(s) ended in "
                                f"runner_system_failure",
                "notify_message": f"{now['running']} running, {now['waiting']} "
                                  f"pending — {now['status_phrase']}",
            })
        # Recovery needs positive evidence of a CLEAN completion, not merely
        # `recent_jobs > 0` — that count is every finished job regardless of
        # outcome, so a runner dropping from 3 systemic failures to 2 (still
        # below `_SYSTEMIC_FAILURE_THRESHOLD`, still failing every job it
        # completes) satisfied it and fired a recovery notice claiming "none
        # ending in runner_system_failure" while two just had (self-review
        # finding, #2296). Gate on the window actually reaching zero, not on
        # the threshold it crossed to get flagged in the first place — the
        # window merely aging out with nothing else happening is still not
        # evidence either way (see `superseded`/`runner_silent` above for the
        # same asymmetry).
        elif (was.get("systemically_failing") and now["systemic_failures"] == 0
              and now["recent_jobs"] > 0):
            events.append({
                "event": "runner_recovered_systemically",
                "payload": {"runner_id": rid, "description": name, **counts},
                "notify_title": f"runner {name} recovered — completing jobs again",
                "notify_message": f"{now['recent_jobs']} job(s) completed recently, "
                                  f"none ending in runner_system_failure",
            })

    for rid, was in previous.items():
        if rid not in current:
            events.append({
                "event": "runner_vanished",
                "payload": {"runner_id": rid, "description": was["description"]},
                "notify_title": f"runner {was['description']} left the fleet",
                "notify_message": "no longer listed for this project",
            })

    return events


def _owners_line(runners: list[dict], tags: str) -> str:
    who = runners_op._owner_names(runners, tags)
    return who or "no runner carries these tags at all"


def _queue_events(
    previous: dict[str, int], current: dict[str, int],
    previous_unknown: dict[str, int], current_unknown: dict[str, int],
    runners: list[dict],
) -> list[dict]:
    """Starvation appearing, deepening, or clearing — and the third state.

    Emitted on the first tick too — see the module docstring. A deepening queue
    re-fires only when it grows, so a stuck backlog does not notify every
    minute for as long as it stays stuck.

    `runner_starved` states a fault and now only fires where there is one:
    every runner that may take the work is down by GitLab's own reckoning, or
    no runner carries the tags. Work waiting on a runner GitLab still calls
    online, stale only by the throttled heartbeat, gets
    `queue_liveness_unknown` instead — the same queue, disclosed, with the
    verdict withheld rather than invented (#750). Dropping it would trade a
    false alarm for a false all-clear, which is the worse of the two.
    """
    events: list[dict] = []

    for tags, count in current.items():
        if count <= previous.get(tags, 0):
            continue
        who = _owners_line(runners, tags)
        events.append({
            "event": "runner_starved",
            "payload": {"tags": tags, "pending": count, "owners": who},
            "notify_title": f"{count} job(s) stuck on [{tags}]",
            "notify_message": who,
        })

    for tags, count in current_unknown.items():
        if count <= previous_unknown.get(tags, 0):
            continue
        who = _owners_line(runners, tags)
        events.append({
            "event": "queue_liveness_unknown",
            "payload": {"tags": tags, "pending": count, "owners": who},
            "notify_title": f"{count} job(s) waiting on [{tags}] — runner liveness UNKNOWN",
            "notify_message": f"{who}; GitLab reports them online and only the "
                              f"throttled contacted_at says otherwise",
        })

    # A tag clears when it has left both buckets. Judged on the union so a
    # queue merely moving between them does not read as drained.
    was_waiting = {**previous_unknown, **previous}
    for tags, before in was_waiting.items():
        if before and not current.get(tags) and not current_unknown.get(tags):
            events.append({
                "event": "queue_cleared",
                "payload": {"tags": tags, "was_pending": before},
                "notify_title": f"[{tags}] queue cleared",
                "notify_message": f"{before} job(s) drained",
            })

    return events


def poll(state: dict, ctx: dict) -> tuple[list[dict], dict]:
    fetched = _fetch_fleet()
    if fetched is None:
        return [], state  # transient — try again next tick

    runners, pending, running = fetched
    first_tick = not state

    current_fleet = {str(r["id"]): _snapshot(r, pending or [], running, runners)
                     for r in runners}
    previous_fleet = state.get("runners") or {}

    events = _fleet_events(previous_fleet, current_fleet, first_tick,
                           queue_known=pending is not None)

    new_state: dict[str, Any] = {"runners": current_fleet}

    if pending is None:
        # Queue unknown this tick — carry the last reading forward untouched so
        # an API blip cannot be read as either starvation or its resolution.
        new_state["starved"] = state.get("starved") or {}
        new_state["unknown"] = state.get("unknown") or {}
        new_state["queue_known"] = False
    else:
        current_starved, current_unknown = runners_op.classify_queue(runners, pending)
        events += _queue_events(state.get("starved") or {}, current_starved,
                                state.get("unknown") or {}, current_unknown, runners)
        new_state["starved"] = current_starved
        new_state["unknown"] = current_unknown
        new_state["queue_known"] = True
        new_state["pending_total"] = len(pending)

    silent = [s["description"] for s in current_fleet.values() if not s["responsive"]]
    new_state["silent"] = silent
    new_state["superseded"] = [s["description"] for s in current_fleet.values()
                               if s.get("superseded")]
    new_state["unconfirmed"] = [s["description"] for s in current_fleet.values()
                                if s.get("unconfirmed")]
    new_state["fleet_size"] = len(current_fleet)

    return events, new_state


def is_terminal(state: dict) -> bool:
    """Never. A fleet has no end state — the watcher runs until unwatched."""
    return False
