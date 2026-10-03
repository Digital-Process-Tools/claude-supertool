











































from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any





INTERVAL = 60











_SYSTEMIC_FAILURE_THRESHOLD = 3

_PRESETS_DIR = Path(__file__).parents[3]


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module





runners_op = _load("watch_gl_runners_op", _PRESETS_DIR / "gitlab" / "runners.py")


def _fetch_fleet() -> tuple[list[dict], list[dict] | None, list[dict]] | None:




















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



    runners_op.annotate_systemic_failures(merged, finished)

    pending, err_pending = runners_op._api(
        "projects/:id/jobs?scope[]=pending&per_page=100", paginate=True
    )
    if err_pending:


        return merged, None, running
    return merged, (pending or []), running


def _snapshot(runner: dict, pending: list[dict], running: list[dict],
              fleet: list[dict]) -> dict[str, Any]:











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









        "blocked": (not responsive) and stuck > 0,
        "unconfirmed": (not responsive) and unproven > 0,




        "superseded": (not responsive) and waiting > 0 and stranded == 0,
    }


def _fleet_events(
    previous: dict[str, dict], current: dict[str, dict], first_tick: bool,
    queue_known: bool = True,
) -> list[dict]:







    if first_tick:
        return []

    events: list[dict] = []

    for rid, now in current.items():
        was = previous.get(rid)
        name = now["description"]

        counts = {"pending_for_it": now["waiting"], "running_on_it": now["running"],
                  "completed_recently": now.get("recent_jobs", 0)}






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
        return [], state  

    runners, pending, running = fetched
    first_tick = not state

    current_fleet = {str(r["id"]): _snapshot(r, pending or [], running, runners)
                     for r in runners}
    previous_fleet = state.get("runners") or {}

    events = _fleet_events(previous_fleet, current_fleet, first_tick,
                           queue_known=pending is not None)

    new_state: dict[str, Any] = {"runners": current_fleet}

    if pending is None:


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

    return False
