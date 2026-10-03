#!/usr/bin/env python3

























from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _console import use_utf8_stdout  
import _untrusted  
import _auth_probe  
import _status_probe  










_HEARTBEAT_WARN_SECONDS = 1800





_STARVED_MIN_QUEUE_SECONDS = 300






_THROUGHPUT_WINDOW_SECONDS = 1800




















_CLOCK_SKEW_TOLERANCE_SECONDS = 60








_LONG_JOB_ALLOWANCE_SECONDS = 4 * 3600





_MAX_HISTORY_PAGES = 5

_MODES = {"full", "queue"}

_MAX_DETAIL_WORKERS = 8


def _format_error(stderr: str, resource: str) -> str:

    s = stderr.lower()
    if _status_probe.says_not_found(s):
        return f"ERROR: {resource} not found. Verify you're in the right repo."



    if _auth_probe.says_not_authenticated(s, _auth_probe.GITLAB_MARKERS):
        return "ERROR: glab not authenticated. Run: glab auth login"
    if _status_probe.says_forbidden(s):
        return (
            f"ERROR: permission denied reading {resource}. Instance-wide runner data "
            "needs admin; project-scoped runners need Maintainer."
        )

    return (f"ERROR: glab failed reading {resource}: "
            f"{_untrusted.flat(stderr.strip())}")


def _parse_paginated_json(raw: str) -> list[dict]:





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

    cmd = ["glab", "api", endpoint]
    if paginate:
        cmd.append("--paginate")
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        return None, "ERROR: glab not found — install the GitLab CLI"
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

    if not timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - parsed).total_seconds()


def _usable_age(seconds: float | None) -> bool:









    return seconds is not None and seconds >= -_CLOCK_SKEW_TOLERANCE_SECONDS


def _human_age(seconds: float | None) -> str:

    if seconds is None:
        return "-"
    if not _usable_age(seconds):



        return "ahead of our clock"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.0f}h"
    return f"{seconds / 86400:.0f}d"


def _fetch_details(runners: list[dict]) -> dict[int, dict]:





    def one(runner: dict) -> tuple[int, dict]:
        data, _err = _api(f"runners/{runner['id']}", timeout=15)
        return runner["id"], (data or {})

    workers = min(_MAX_DETAIL_WORKERS, max(1, len(runners)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(pool.map(one, runners))


def _can_serve(runner: dict, job_tags: list[str]) -> bool:




    runner_tags = set(runner.get("tag_list") or [])
    if not job_tags:
        return bool(runner.get("run_untagged"))
    return set(job_tags).issubset(runner_tags)





_GITLAB_DOWN_STATUSES = {"offline", "stale", "never_contacted"}


def _demonstrably_down(runner: dict) -> bool:











    if runner.get("paused") or not runner.get("active", True):
        return True
    return runner.get("status") in _GITLAB_DOWN_STATUSES


def status_phrase(runner: dict) -> str:








    if runner.get("paused"):
        return "GitLab reports it paused — un-pause it, or move the tag to a live runner"
    if not runner.get("active", True):
        return "GitLab reports it inactive"
    status = runner.get("status")
    if status in _GITLAB_DOWN_STATUSES:
        return f"GitLab reports it {status} — the registration may simply need deleting"
    return "GitLab still reports it online"




_LIVE_JOBS_MARK = "_live_jobs_checked"


class UnannotatedFleetError(RuntimeError):
    pass















def _missing_annotations(runner: dict) -> list[str]:






    missing = []
    if "_recent_jobs" not in runner:
        missing.append("annotate_recent_work")
    if not runner.get(_LIVE_JOBS_MARK):
        missing.append("annotate_live_jobs")
    return missing


def _liveness_unknown(runner: dict) -> bool:















    return not _demonstrably_down(runner) and not _is_responsive(runner)


def _is_responsive(runner: dict) -> bool:


























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


    return _usable_age(age) and age <= _HEARTBEAT_WARN_SECONDS


def fetch_recent_finished(
    window_seconds: int = _THROUGHPUT_WINDOW_SECONDS,
) -> tuple[list[dict], bool]:









    collected: list[dict] = []
    truncated = True
    for page in range(1, _MAX_HISTORY_PAGES + 1):
        batch, err = _api(f"projects/:id/jobs?per_page=100&page={page}")
        if err or not batch:
            truncated = False
            break
        collected.extend(batch)
        oldest = _age_seconds(batch[-1].get("created_at"))


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

    done: dict[int, int] = {}
    for job in finished:
        rid = (job.get("runner") or {}).get("id")
        if rid is not None:
            done[rid] = done.get(rid, 0) + 1
    for runner in runners:
        runner["_recent_jobs"] = done.get(runner.get("id"), 0)
    return runners






RUNNER_SYSTEM_FAILURE = "runner_system_failure"


def annotate_systemic_failures(runners: list[dict], finished: list[dict]) -> list[dict]:








    failed: dict[int, int] = {}
    for job in finished:
        if job.get("failure_reason") != RUNNER_SYSTEM_FAILURE:
            continue
        rid = (job.get("runner") or {}).get("id")
        if rid is not None:
            failed[rid] = failed.get(rid, 0) + 1
    for runner in runners:
        runner["_systemic_failures"] = failed.get(runner.get("id"), 0)
    return runners


def annotate_live_jobs(runners: list[dict],
                       running: list[dict] | None) -> list[dict]:

















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
































    stuck: dict[str, int] = {}
    unproven: dict[str, int] = {}
    for job in pending:
        queued = _age_seconds(job.get("created_at"))







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







    return classify_queue(runners, pending)[0]


def waiting_for(runner: dict, pending: list[dict]) -> int:

    return sum(1 for job in pending if _can_serve(runner, job.get("tag_list") or []))


def stranded_for(runner: dict, pending: list[dict], fleet: list[dict]) -> int:























    return sum(stranded_split_for(runner, pending, fleet))


def stranded_split_for(
    runner: dict, pending: list[dict], fleet: list[dict]
) -> tuple[int, int]:















    live = [r for r in fleet if _is_responsive(r)]
    stuck = 0
    unproven = 0
    for job in pending:
        queued = _age_seconds(job.get("created_at"))



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





RADAR_OPTIONS = {"window", "quiet_when_healthy"}




RADAR_QUIET_DEFAULT = True






WATCH_SOURCE = "gl-runners"
WATCH_SCOPE = "fleet"


def radar_report(options: dict | None = None) -> tuple[list[str], bool]:













    options = options or {}
    window = options.get("window", _THROUGHPUT_WINDOW_SECONDS)
    watch = options.get("_watch")
    if watch is not None:
        watch(WATCH_SOURCE, WATCH_SCOPE)

    listed, err = _api("projects/:id/runners?per_page=100", paginate=True)
    if err and _status_probe.says_forbidden(err):
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




        return ([f"radar: WARNING — the running-jobs list is unreadable "
                 f"({err_running}). It is the strongest liveness evidence there "
                 f"is, so runner health is UNKNOWN, not green."], False)
    annotate_live_jobs(runners, running or [])



    if pending:
        annotate_recent_work(runners, fetch_recent_finished(window)[0])

    blocked, unproven = classify_queue(runners, pending)

    if not blocked and not unproven and not pending:











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

    running_by_runner: dict[int, int] = {}
    for job in running:
        rid = (job.get("runner") or {}).get("id")
        if rid is not None:
            running_by_runner[rid] = running_by_runner.get(rid, 0) + 1



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

    owners = [r for r in runners
              if _can_serve(r, tags.split(",") if tags != "(untagged)" else [])]
    return ", ".join(
        f"{_untrusted.flat(str(r.get('description')))} "
        f"(seen {_human_age(_age_seconds(r.get('contacted_at')))} ago)"
        for r in owners
    )


def _print_diagnosis(runners: list[dict], pending: list[dict]) -> None:








    blocked, unproven = classify_queue(runners, pending)

    if not blocked and not unproven:





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



            print(f"    version     : {_untrusted.flat(str(version))}  "
                  f"{_untrusted.flat(str(runner.get('platform') or ''))} "
                  f"{_untrusted.flat(str(runner.get('architecture') or ''))}".rstrip())


def main() -> int:
    use_utf8_stdout()
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





        print(f"ERROR: the running-jobs list is unreadable — {err_running}. "
              f"Every liveness verdict in this view is derived from it. Printing "
              f"the fleet without it would report runners executing jobs as "
              f"unaccounted for.")
        return 1

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
