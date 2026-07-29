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
    silent   not taking work AND work is waiting for it — a wedge
    starved  pending jobs whose tags match only wedged runners
    fleet    membership and paused-flag changes

`runner_silent` gates on consequence, and that is not a refinement — it is the
whole difference between a signal and noise. Built on heartbeat staleness alone
it fired on 6 of 6 online runners in its first hour, because GitLab throttles
its contacted_at writes and a runner taking jobs continuously still reads
minutes stale. A signal that fires on the entire healthy fleet does not just
fail to inform, it buries the one event that was real. So liveness is judged
first on job_execution_status and the running-jobs list — direct proof — and
silence is only reported when jobs are actually queued behind it.

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
    """
    listed, err = runners_op._api("projects/:id/runners?per_page=100", paginate=True)
    if err or listed is None:
        return None

    details = runners_op._fetch_details(listed)
    merged = [{**runner, **details.get(runner["id"], {})} for runner in listed]

    running, err_running = runners_op._api(
        "projects/:id/jobs?scope[]=running&per_page=100", paginate=True)
    running = [] if err_running else (running or [])
    runners_op.annotate_live_jobs(merged, running)
    runners_op.annotate_recent_work(merged, runners_op.fetch_recent_finished()[0])

    pending, err_pending = runners_op._api(
        "projects/:id/jobs?scope[]=pending&per_page=100", paginate=True
    )
    if err_pending:
        # The fleet half is still usable; treat the queue as unknown rather
        # than empty, so a queue outage cannot read as "starvation cleared".
        return merged, None, running
    return merged, (pending or []), running


def _snapshot(runner: dict, pending: list[dict], running: list[dict]) -> dict[str, Any]:
    """The fields whose changes are worth an event.

    `waiting` and `running` ride on every snapshot rather than only the starved
    one: those two counts are exactly what separates a runner that is idle from
    one that is wedged, and leaving them off forces whoever reads the event to
    go and fetch them by hand before they can act on it.
    """
    responsive = runners_op._is_responsive(runner)
    waiting = runners_op.waiting_for(runner, pending)
    return {
        "description": runner.get("description") or f"#{runner.get('id')}",
        "responsive": responsive,
        "paused": bool(runner.get("paused")),
        "tags": sorted(runner.get("tag_list") or []),
        "contacted_at": runner.get("contacted_at"),
        "waiting": waiting,
        "running": sum(1 for job in running
                       if (job.get("runner") or {}).get("id") == runner.get("id")),
        "recent_jobs": runner.get("_recent_jobs", 0),
        # Silence is only news when work is stuck behind it. A quiet runner with
        # an empty queue is a runner with nothing to do, and alerting on that
        # fires on the whole fleet every time the pipeline goes idle.
        "blocked": (not responsive) and waiting > 0,
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

        if was is None:
            events.append({
                "event": "runner_added",
                "payload": {"runner_id": rid, "description": name, "tags": now["tags"],
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
                            "tags": now["tags"], **counts},
                "notify_title": f"runner {name} wedged — {now['waiting']} job(s) waiting",
                "notify_message": f"last contact {age} ago, {now['running']} running "
                                  f"— GitLab still reports it online",
            })
        elif queue_known and was.get("blocked") and now["responsive"]:
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

    for rid, was in previous.items():
        if rid not in current:
            events.append({
                "event": "runner_vanished",
                "payload": {"runner_id": rid, "description": was["description"]},
                "notify_title": f"runner {was['description']} left the fleet",
                "notify_message": "no longer listed for this project",
            })

    return events


def _queue_events(
    previous: dict[str, int], current: dict[str, int], runners: list[dict]
) -> list[dict]:
    """Starvation appearing, deepening, or clearing.

    Emitted on the first tick too — see the module docstring. A deepening queue
    re-fires only when it grows, so a stuck backlog does not notify every
    minute for as long as it stays stuck.
    """
    events: list[dict] = []

    for tags, count in current.items():
        before = previous.get(tags, 0)
        if count <= before:
            continue

        owners = [
            r for r in runners
            if runners_op._can_serve(r, [] if tags == "(untagged)" else tags.split(","))
        ]
        if owners:
            who = ", ".join(
                f"{r.get('description')} (seen "
                f"{runners_op._human_age(runners_op._age_seconds(r.get('contacted_at')))} ago)"
                for r in owners
            )
        else:
            who = "no runner carries these tags at all"

        events.append({
            "event": "runner_starved",
            "payload": {"tags": tags, "pending": count, "owners": who},
            "notify_title": f"{count} job(s) stuck on [{tags}]",
            "notify_message": who,
        })

    for tags, before in previous.items():
        if before and not current.get(tags):
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

    current_fleet = {str(r["id"]): _snapshot(r, pending or [], running)
                     for r in runners}
    previous_fleet = state.get("runners") or {}

    events = _fleet_events(previous_fleet, current_fleet, first_tick,
                           queue_known=pending is not None)

    new_state: dict[str, Any] = {"runners": current_fleet}

    if pending is None:
        # Queue unknown this tick — carry the last reading forward untouched so
        # an API blip cannot be read as either starvation or its resolution.
        new_state["starved"] = state.get("starved") or {}
        new_state["queue_known"] = False
    else:
        current_starved = runners_op.starved_tags(runners, pending)
        events += _queue_events(state.get("starved") or {}, current_starved, runners)
        new_state["starved"] = current_starved
        new_state["queue_known"] = True
        new_state["pending_total"] = len(pending)

    silent = [s["description"] for s in current_fleet.values() if not s["responsive"]]
    new_state["silent"] = silent
    new_state["fleet_size"] = len(current_fleet)

    return events, new_state


def is_terminal(state: dict) -> bool:
    """Never. A fleet has no end state — the watcher runs until unwatched."""
    return False
