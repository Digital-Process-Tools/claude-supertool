#!/usr/bin/env python3
























from __future__ import annotations

import calendar
import importlib.util
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any


sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))  
from _console import use_utf8_stdout  
import _st_hint  
import _untrusted  
import naming  
import ratelimit  
import sourcepath  
import transport  


def _load_source(name: str, resolved: "sourcepath.Resolved | None" = None):








    poller_path, _origin = sourcepath.find(name, resolved)
    if poller_path is None:
        return None
    spec = importlib.util.spec_from_file_location(f"watch_source_{name}", poller_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module




LOOKUP_ONLY_STATE_KEYS = {"lookup", "error"}


def _is_bootstrap_state(state: dict) -> bool:








    return not state or set(state) <= LOOKUP_ONLY_STATE_KEYS


def _parse_args(parts: list[str]) -> tuple[str, str, list[str]]:








    if len(parts) < 1 or not parts[0]:
        raise ValueError("missing SOURCE")
    if len(parts) < 2 or not parts[1]:
        raise ValueError(f"missing ID for source {parts[0]!r}")
    source, watcher_id = parts[0], parts[1]


    if "__" in source or "__" in watcher_id:
        raise ValueError("SOURCE and ID must not contain '__' (reserved as filename separator)")


    if "/" in source or "/" in watcher_id:
        raise ValueError("SOURCE and ID must not contain '/' (they are filename components)")
    only: list[str] = []
    for p in parts[2:]:
        if p.startswith("only="):
            only = [e for e in p[len("only="):].split(",") if e]
    return source, watcher_id, only


def cmd_watch(parts: list[str]) -> int:
    try:
        source, watcher_id, only = _parse_args(parts)
    except ValueError as e:
        print(f"ERROR: {e}")
        return 1






    if "reload" in parts[2:]:
        return cmd_reload(source, watcher_id)
    resolved = sourcepath.resolve()



    poller = _load_source(source, resolved)
    if poller is None:





        print(f"ERROR: unknown source {source!r}. Searched:")
        for line in sourcepath.search_report(resolved):
            print(line)
        return 1




    for line in sourcepath.op_lines("watch", resolved):
        print(f"watch: {line}")
    status, pid = start_poller(source, watcher_id, only)
    if status == "alive":




        print(f"Already watching {source}:{watcher_id} (PID {pid}) — "
              f"not starting a second. "
              f"Use ./supertool 'unwatch:{source}:{watcher_id}' to stop it.")
        return 0
    if status == "failed":
        print(f"ERROR: could not spawn a poller for {source}:{watcher_id}")
        return 1
    if status == "unclaimable":







        print(f"ERROR: could not claim the slot for {source}:{watcher_id} — its "
              f"pid file at "
              f"{naming.flat_path(transport.pid_path(source, watcher_id))} could "
              f"not be created. Nothing was started, and nothing here knows "
              f"whether a poller is already running for this id. Check that "
              f"{naming.flat_path(transport.STATE_DIR)} is a writable directory "
              f"({naming.state_dir_provenance(transport.RESOLVED)}).")
        return 1



    if transport.clear_deaths(source, watcher_id):
        print(f"Cleared the recorded deaths for {source}:{watcher_id} — "
              f"radar will respawn it again if it dies.")
    print(f"Watching {source}:{watcher_id} (PID {pid})")
    if only:
        print(f"Filter: {','.join(only)}")
    print(f"State: {transport.state_path(source, watcher_id)}")
    return 0


def cmd_reload(source: str, watcher_id: str) -> int:



























    for line in sourcepath.op_lines("watch"):
        print(f"watch: {line}")
    if RELOAD_SIGNAL is None:
        print("ERROR: this platform has no SIGHUP, so a poller cannot be "
              "signalled to reload in place. "
              f"{_st_hint.st_hint(f'unwatch:{source}:{watcher_id}')} then "
              f"{_st_hint.st_hint(f'watch:{source}:{watcher_id}')} is the only "
              f"path here, and it costs a full re-import of both poller.py "
              f"and dispatcher.py/transport.py -- the whole reason this op "
              f"exists -- even though state itself is resumed from disk, "
              f"not lost.")
        return 1
    census = transport.poller_census()
    info = transport.watcher_pids(
        source, watcher_id, scan=(census["mine"], census["scan_ok"]))
    pids = [pid for pid in info["pids"] if pid > 1 and pid != os.getpid()]
    if not pids:
        if info.get("tracked_refusal"):
            print(f"No readable PID file for {source}:{watcher_id} -- "
                  f"{info['tracked_refusal']}. Nothing was signalled, and "
                  f"whether a poller holds this slot is not known from here.")
        elif info["tracked"] and not info["tracked_alive"]:
            print(f"Tracked PID {info['tracked']} for {source}:{watcher_id} "
                  f"is not running -- there is nothing here to reload.")
        elif not info["scan_ok"]:
            print(f"No PID file for {source}:{watcher_id}, and the process "
                  f"scan was unavailable -- an untracked poller could not be "
                  f"ruled out. Nothing was signalled.")
        else:
            print(f"No active watcher for {source}:{watcher_id} -- "
                  f"nothing to reload.")
        print(f"Use {_st_hint.st_hint(f'watch:{source}:{watcher_id}')} to start one.")
        return 1
    print(f"Reloading {len(pids)} poller(s) for {source}:{watcher_id}: "
          + ", ".join(
              f"{pid} ({'tracked' if pid == info['tracked'] else 'untracked'})"
              for pid in pids))
    failures = 0
    for pid in pids:
        try:
            os.kill(pid, RELOAD_SIGNAL)
        except ProcessLookupError:
            failures += 1
            print(f"ERROR: PID {pid} is gone -- it exited between the scan "
                  f"above and this signal.")
        except OSError as e:
            failures += 1
            print(f"ERROR: could not signal PID {pid}: {e}")
        else:
            print(f"Signalled PID {pid}. Its own next tick re-imports "
                  f"{source}'s poller.py -- state stays intact, and it keeps "
                  f"polling on today's code until then. A `{RELOAD_FAILED_EVENT}` "
                  f"event means the import failed and it is still on today's "
                  f"code; a `{RELOAD_EVENT}` event confirms the swap.")
    if failures < len(pids):






        print(f"Note: this signal only re-imports {source}'s own poller.py in "
              f"the running process -- dispatcher.py itself (the shared "
              f"back-off/retry/wait machinery every poller runs under: "
              f"_retry_after_seconds, _wait_interruptible, "
              f"MAX_RETRY_AFTER_SECONDS, the outer poll loop) is already "
              f"imported by that same process and is NOT swapped by this "
              f"signal, no matter how many pollers it reaches. If the fix "
              f"you are deploying lives in dispatcher.py rather than in "
              f"{source}'s own poller.py, this reload will not pick it up -- "
              f"{_st_hint.st_hint(f'unwatch:{source}:{watcher_id}')} then "
              f"{_st_hint.st_hint(f'watch:{source}:{watcher_id}')} instead, "
              f"which forks a fresh process and re-imports both -- state "
              f"itself is resumed from disk, not lost.")
    if not info["scan_ok"]:
        print("Process scan unavailable -- only the tracked PID was "
              "considered, so an untracked poller for this id would not "
              "have been signalled.")
    return 1 if failures else 0


def _stop_pid(pid: int) -> str:






    hard = getattr(signal, "SIGKILL", signal.SIGTERM)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return ""
    except OSError as e:
        return str(e)
    for _ in range(10):
        if not transport._pid_alive(pid):
            return ""
        time.sleep(0.05)
    try:
        os.kill(pid, hard)
    except ProcessLookupError:
        return ""
    except OSError as e:
        return str(e)
    time.sleep(0.05)
    return "" if not transport._pid_alive(pid) else "still running after SIGKILL"


def _foreign_slot_lines(census: dict, source: str, watcher_id: str) -> list[str]:













    if not census["scan_ok"]:
        return []
    key = (source, watcher_id)
    other, unknown = census["other"], census["unknown"]
    lines: list[str] = []
    dirs: dict[str, str] | None = None
    dir_state = dir_why = ""
    for channel, slots in sorted(other.items()):
        pids = slots.get(key)
        if not pids:
            continue
        if dirs is None:
            dirs, dir_state, dir_why = transport.channel_dirs()
        if channel in dirs:
            where = f"state dir {naming.flat_path(dirs[channel])}"
        elif dir_state != transport.STATE_DIR_OK:
            where = f"could not be resolved to a directory ({dir_why})"
        else:
            where = (f"no state directory under "
                     f"{naming.flat_path(naming.BASE_DIR)} hashes to it")
        lines.append(f"  {len(pids)} on channel {_untrusted.flat(channel)} — {where}")
    other_lines = list(lines)
    unk_pids = unknown.get(key)
    if unk_pids:
        lines.append(f"  {len(unk_pids)} whose channel cannot be told from "
                     f"their argv (started before the channel token existed)")
    if not lines:
        return []





    header = (f"The process scan also saw poller(s) for {source}:{watcher_id} "
              + ("on another channel, " if other_lines else "whose channel "
                 "could not be established, ")
              + "unaffected by this unwatch:")
    return ([header] + lines
            + ["To act on those, run `unwatch` under the SUPERTOOL_WATCH_NAME "
               "that derives their state dir."])


def _disclose_or_decline_foreign(census: dict[str, Any], source: str,
                                  watcher_id: str) -> None:











    if not census["scan_ok"]:
        print("The process scan for other channels was unavailable, so a "
              "poller covering this slot on another channel could not be "
              "ruled out.")
        return
    for line in _foreign_slot_lines(census, source, watcher_id):
        print(line)


def _report_nothing_stopped(source: str, watcher_id: str, info: dict[str, Any],
                             census: dict[str, Any]) -> None:

    if info.get("tracked_refusal"):



        print(f"No readable PID file for {source}:{watcher_id} — "
              f"{info['tracked_refusal']}. Nothing was stopped, and whether a "
              f"poller holds this slot is not known from here. Inspect the "
              f"path before re-arming.")
        _disclose_or_decline_foreign(census, source, watcher_id)
        return
    if info["tracked"] and not info["tracked_alive"]:
        print(f"Tracked PID {info['tracked']} for {source}:{watcher_id} is not "
              f"running — the watcher died without anything reporting it, and "
              f"this id has been unwatched since. Stale PID file removed.")
        transport.record_death(source, watcher_id, info["tracked"])
        _disclose_or_decline_foreign(census, source, watcher_id)
        return
    if not info["scan_ok"]:
        print(f"No PID file for {source}:{watcher_id}, and the process scan was "
              f"unavailable — a poller that is running untracked could not be "
              f"ruled out. Nothing was stopped.")
        return
    print(f"No active watcher for {source}:{watcher_id} "
          f"(no PID file, and no matching process).")
    for line in _foreign_slot_lines(census, source, watcher_id):
        print(line)


def cmd_unwatch(parts: list[str]) -> int:



























    try:
        source, watcher_id, _ = _parse_args(parts)
    except ValueError as e:
        print(f"ERROR: {e}")
        return 1





    for line in sourcepath.op_lines("unwatch"):
        print(f"unwatch: {line}")




    census = transport.poller_census()
    info = transport.watcher_pids(
        source, watcher_id, scan=(census["mine"], census["scan_ok"]))
    pids = [pid for pid in info["pids"] if pid > 1 and pid != os.getpid()]
    skipped = [pid for pid in info["pids"] if pid not in pids]
    if skipped:
        print("Not signalling " + ", ".join(str(p) for p in skipped)
              + " — a watcher is never PID 1 nor this process.")





    releasable = not info.get("tracked_refusal")
    if not pids:
        _report_nothing_stopped(source, watcher_id, info, census)
        if releasable:
            transport.release_pidfile(source, watcher_id)
        _acknowledge_deaths(source, watcher_id)
        return 0
    print(f"Stopping {len(pids)} poller(s) for {source}:{watcher_id}: "
          + ", ".join(
              f"{pid} ({'tracked' if pid == info['tracked'] else 'untracked'})"
              for pid in pids))
    failures = 0
    for pid in pids:
        why = _stop_pid(pid)
        if why:
            failures += 1
            print(f"ERROR: could not stop PID {pid}: {why}")
        else:
            print(f"Stopped PID {pid}.")
    if not info["scan_ok"]:
        print("Process scan unavailable — only the tracked PID was considered, "
              "so an untracked poller for this id would not have been found.")
    if releasable:
        transport.release_pidfile(source, watcher_id)
    else:
        print(f"The PID file for {source}:{watcher_id} was left in place — "
              f"{info['tracked_refusal']}. The pollers above were stopped; the "
              f"slot stays unclaimable until somebody removes that path by hand.")
    _acknowledge_deaths(source, watcher_id)
    return 1 if failures else 0


def _acknowledge_deaths(source: str, watcher_id: str) -> None:







    if transport.clear_deaths(source, watcher_id):
        print(f"Acknowledged the recorded death(s) for {source}:{watcher_id}.")


def _row_note(row: dict[str, Any]) -> str:
    notes = []
    if row.get("state_refusal"):





        notes.append(f"state unread — {row['state_refusal']}")
    if row.get("dead"):
        recorded = row.get("deaths") or []
        last = recorded[-1].get("pid") if recorded else "?"
        notes.append(f"LOST — PID {last} died, no poller since"
                     + (f" ({len(recorded)} deaths recorded)" if len(recorded) > 1 else ""))
    elif len(row.get("deaths") or []) > 1:




        notes.append(f"flapping — {len(row['deaths'])} deaths recorded, currently respawned")
    if row.get("orphan"):
        notes.append("no pidfile")
    if row.get("extra"):
        notes.append(f"{len(row['pids'])} live pollers: "
                     + ", ".join(str(p) for p in row["pids"]))
    return "; ".join(notes)


def _scan_unavailable_reason() -> str:






    if not transport.ps_scan_supported():
        return ("This machine's process scan cannot answer — either there is "
                "no `ps` here, or the one there is does not accept the "
                "invocation the scan makes. So an untracked or duplicate "
                "poller can never be seen here and `radar` cannot reap one. "
                "That is permanent, which is why radar does not repeat it on "
                "every run — this line is the disclosure.")
    return ("The scan could not be read this time, though `ps` is present. "
            "Run it again; if it keeps failing, nothing is watching for "
            "duplicate pollers.")


def _foreign_poller_lines(census: dict) -> list[str]:



















    if not census["scan_ok"]:
        return []
    other, unknown = census["other"], census["unknown"]
    if not other and not unknown:
        return []
    dirs, dir_state, dir_why = transport.channel_dirs()
    total = sum(len(p) for slots in other.values() for p in slots.values())
    total += sum(len(p) for p in unknown.values())
    out = [f"the process scan also saw {total} labelled poller(s) that this "
           f"board may not list or stop:"]




    installed, _installed_why = transport.installed_version()
    other_versions = census.get("other_versions", {})
    for channel, slots in sorted(other.items()):
        count = sum(len(pids) for pids in slots.values())
        if channel in dirs:
            where = f"state dir {naming.flat_path(dirs[channel])}"
        elif dir_state != transport.STATE_DIR_OK:


            where = f"could not be resolved to a directory ({dir_why})"
        else:
            where = (f"no state directory under "
                     f"{naming.flat_path(naming.BASE_DIR)} hashes to it")
        version_line = transport.foreign_version_disclosure(
            other_versions.get(channel, []), installed)
        out.append(f"  {count} on channel {_untrusted.flat(channel)}, "
                   f"{len(slots)} slot(s) — {where}, {version_line}")
    if unknown:
        count = sum(len(pids) for pids in unknown.values())
        out.append(f"  {count} whose channel cannot be told from their argv "
                   f"(started before the channel token existed), "
                   f"{len(unknown)} slot(s)")
    out.append("`unwatch` here reaches only this channel's slots. To act on "
               "another channel's, run `watches` under the "
               "SUPERTOOL_WATCH_NAME that derives its state dir.")
    return out


def _active_gh_rate_limit_errors(
    rows: list[dict[str, Any]],
) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
















    states: dict[tuple[str, str], dict[str, Any]] = {}
    unreadable: list[tuple[str, str, str]] = []
    for row in rows:
        source = str(row.get("source", ""))
        if source not in ratelimit.GH_SOURCES:
            continue
        watcher_id = str(row.get("id", ""))
        state, refusal = transport.read_state_checked(source, watcher_id)
        if refusal:
            unreadable.append((source, watcher_id, refusal))
            continue
        states[(source, watcher_id)] = state or {}
    return ratelimit.active_gh_rate_limit_errors(states), unreadable


def cmd_list() -> int:














    for line in transport.channel_disclosure():
        print(f"watches: {line}")
    for line in sourcepath.op_lines("watches"):
        print(f"watches: {line}")
    census = transport.poller_census()
    rows, scan_ok = transport.list_watchers(census)



    foreign = _foreign_poller_lines(census)
    for line in foreign:
        print(f"watches: {line}")






    if census.get("scan_ok"):




        fleet_interval = interval_override()
        interval_by_source = ({source: fleet_interval for source in ratelimit.GH_SOURCES}
                              if fleet_interval else None)













        projected, gh_counts = ratelimit.fleet_projected_requests_per_hour(
            census, interval_by_source=interval_by_source)
        rate_limit, rate_limit_why = ratelimit.read_rate_limit()







        active_errors, unreadable = _active_gh_rate_limit_errors(rows)
        for line in ratelimit.render_budget_lines(rate_limit, rate_limit_why,
                                                   projected, gh_counts,
                                                   active_errors=active_errors,
                                                   unreadable_states=unreadable):
            print(f"watches: {line}")
    dir_state, dir_why = transport.state_dir_status()
    if dir_state == transport.STATE_DIR_UNREADABLE:



        print(f"WARNING — {dir_why}, so the poller slots recorded there could "
              f"not be enumerated. This board is built from what the process "
              f"scan found and nothing else; it is not evidence of absence.")
    if not rows:
        if dir_state == transport.STATE_DIR_ABSENT:




            print(f"No watchers — the state directory "
                  f"{naming.flat_path(transport.STATE_DIR)} does "
                  f"not exist yet, so nothing has ever spawned on this channel "
                  f"({naming.state_dir_provenance(transport.RESOLVED)}). The "
                  f"first `watch:SOURCE:ID` or `radar` spawn creates it; no read "
                  f"path does.")
            if not scan_ok:
                print(_scan_unavailable_reason())
            return 0
        if dir_state == transport.STATE_DIR_UNREADABLE:





            if not scan_ok:
                print(_scan_unavailable_reason())
            return 0
        if not scan_ok:
            print("No watchers by PID file — and the process scan was "
                  "unavailable, so an untracked poller could not be ruled out.")
            print(_scan_unavailable_reason())
            return 0
        if foreign:






            print("No watchers on this channel. None recorded as lost either.")
            return 0
        print("No active watchers. None recorded as lost either.")
        return 0
    for r in rows:
        r["_pid"] = ("-" if r.get("dead") else
                     str(r["pid"]) + (f" (+{len(r['extra'])})" if r["extra"] else ""))


















        r["_source"] = _untrusted.flat(r["source"])
        r["_id"] = _untrusted.flat(r["id"])
        r["_last_event"] = _untrusted.flat(r["last_event"] or "-")
        r["_note"] = _untrusted.flat(_row_note(r))
        r["_started"] = _untrusted.flat(r["started"] or "-")






        r["_delivery_state"] = transport.delivery_of(
            r.get("last_emit"), r.get("state_refusal") or "")
        r["_delivery"] = transport.DELIVERY_LABELS[r["_delivery_state"]]






        r["_version_state"], r["_version_why"] = transport.version_state_of(
            r.get("forked_fingerprint"), r.get("forked_fingerprint_error"),
            r.get("reloaded_at"), r.get("reloaded_fingerprint"),
            r.get("reloaded_fingerprint_error"))
        r["_version"] = transport.VERSION_LABELS[r["_version_state"]]
    noted = any(r["_note"] for r in rows)
    widths = {
        "source": max(6, max(len(r["_source"]) for r in rows)),
        "id": max(2, max(len(r["_id"]) for r in rows)),
        "pid": max(5, max(len(r["_pid"]) for r in rows)),
        "started": 20,
        "last_event": max(10, max(len(r["_last_event"]) for r in rows)),
        "delivery": max(8, max(len(r["_delivery"]) for r in rows)),
        "version": max(7, max(len(r["_version"]) for r in rows)),
    }
    header = (
        f"{'SOURCE':<{widths['source']}}  "
        f"{'ID':<{widths['id']}}  "
        f"{'PID':<{widths['pid']}}  "
        f"{'STARTED':<{widths['started']}}  "
        f"{'LAST_EVENT':<{widths['last_event']}}  "
        f"{'DELIVERY':<{widths['delivery']}}  "
        f"{'VERSION':<{widths['version']}}"
    )
    if noted:
        header += "  NOTE"






    print(_untrusted.flat_note("the SOURCE, ID and LAST_EVENT columns",
                               "the pollers' own state files and filenames"))
    print(header)
    print("-" * len(header))
    for r in rows:
        line = (
            f"{r['_source']:<{widths['source']}}  "
            f"{r['_id']:<{widths['id']}}  "
            f"{r['_pid']:<{widths['pid']}}  "
            f"{r['_started']:<{widths['started']}}  "
            f"{r['_last_event']:<{widths['last_event']}}  "
            f"{r['_delivery']:<{widths['delivery']}}  "
            f"{r['_version']:<{widths['version']}}"
        )
        if noted:
            line += f"  {r['_note']}"
        print(line.rstrip())
    unread = [r for r in rows if r.get("state_refusal")]
    if unread:
        print()
        print(f"{len(unread)} row(s) above are marked `state unread`: the state "
              f"file is there and could not be read, so this board knows nothing "
              f"about their last event, and — for a row with no live poller — "
              f"nothing about whether the watcher was lost. That is a different "
              f"fact from a quiet watcher. A state file is written in place by "
              f"its own poller and lives in a directory anyone on this machine "
              f"can write to; inspect it before re-arming.")







    stranded = [r for r in rows if r["_delivery_state"] == transport.EMIT_NO_LISTENER]
    if stranded:
        print()
        print(f"{len(stranded)} row(s) above are marked NO LISTENER: that watcher's "
              f"own last emit found nothing bound to the socket, so the events it "
              f"reported went nowhere. The poller is fine; this is about the other "
              f"end. A session started without `--dangerously-load-development-"
              f"channels server:claude-channel` binds no reader at all, and then "
              f"this is the expected state rather than a fault. `channel:health` "
              f"is the judgement about the socket itself. Do not stop or re-arm a "
              f"watcher on the strength of this column.")
    undecided = [r for r in rows if r["_delivery_state"] == transport.EMIT_UNKNOWN]
    if undecided:
        print()
        print(f"{len(undecided)} row(s) above are marked `unknown` in DELIVERY: the "
              f"last emit settled nothing either way — this platform has no AF_UNIX "
              f"socket, or the write failed for a reason that decides nothing, or "
              f"the state file itself could not be read. It is not a pass.")





    stale = [r for r in rows if r["_version_state"] == transport.VERSION_STALE]
    if stale:
        print()
        print(f"{len(stale)} row(s) above are marked STALE in VERSION: this "
              f"poller forked before the source under presets/watch/ last "
              f"changed, so it is running code a later fix may have replaced. "
              f"`watch:SOURCE:ID:reload` picks it up in place, state intact "
              f"(#2212); `unwatch:SOURCE:ID` then `watch:SOURCE:ID` also works "
              f"and resumes the same state from disk (#2697) -- it is a fresh "
              f"process, not a fresh baseline. Nothing here restarts it "
              f"automatically.")
        for r in stale:
            print(f"  {r['_source']}:{r['_id']} — {r['_version_why']}")




    reloaded = [r for r in rows if r["_version_state"] == transport.VERSION_RELOADED]
    if reloaded:
        print()
        print(f"{len(reloaded)} row(s) above are marked RELOADED in VERSION: "
              f"`:reload` already ran and confirmed the swap, but it only "
              f"re-imports poller.py in place -- dispatcher.py and "
              f"transport.py in that running process are still the fork-time "
              f"copies and cannot be swapped that way. "
              f"`watch:SOURCE:ID:reload` again will not "
              f"change that. `unwatch:SOURCE:ID` then `watch:SOURCE:ID` is "
              f"the only way to a fully current process for that row (it "
              f"forks a fresh one that resumes the prior state from disk, so "
              f"nothing is re-announced unless something actually changed).")
        for r in reloaded:
            print(f"  {r['_source']}:{r['_id']} — {r['_version_why']}")
    version_unknown = [r for r in rows if r["_version_state"] == transport.VERSION_UNKNOWN]
    if version_unknown:
        print()
        print(f"{len(version_unknown)} row(s) above are marked `unknown` in "
              f"VERSION: whether this poller's code is current was not "
              f"established — it predates #2179, its fingerprint could not be "
              f"read, or this render could not read its own source. This is "
              f"not the same claim as `current`.")
    lost = [r for r in rows if r.get("dead")]
    if lost:
        print()
        print(f"{len(lost)} id(s) above are marked LOST: they had a watcher, it "
              f"died without being unwatched, and nothing is polling them now. "
              f"Events on those ids are not being seen. Re-arm with "
              f"`watch:SOURCE:ID` (radar heals them automatically up to "
              f"{transport.DEATH_RESPAWN_LIMIT} deaths), or acknowledge with "
              f"`unwatch:SOURCE:ID` to drop the row.")



    if any(r.get("orphan") or r.get("extra") for r in rows):
        print()
        print("An id above has more than one live poller, or a poller with no "
              "PID file. `unwatch:SOURCE:ID` stops all of them and names each "
              "one. Do not identify a watcher from `ps` — see "
              "docs/presets/watch.md.")
    if not scan_ok:
        print()
        print("Process scan unavailable — only pidfile-tracked pollers are "
              "listed here; untracked ones were not checked.")
        print(_scan_unavailable_reason())
    return 0


def reap_duplicate_pollers() -> list[str]:
























































    found, scan_ok = transport.scan_poller_pids()
    if not scan_ok:
        if not transport.ps_scan_supported():









            return []
        return ["radar: reap skipped — the process scan was unavailable, so a "
                "duplicate poller could not be ruled out. Nothing was stopped, "
                "and an id may be emitting every event more than once."]

    lines: list[str] = []
    for (source, watcher_id), pids in sorted(found.items()):
        live = sorted(pid for pid in pids
                      if pid > 1 and pid != os.getpid() and transport._pid_alive(pid))
        if len(live) < 2:
            continue
        tracked, tracked_refusal = transport.read_pid_checked(source, watcher_id)
        tracked = tracked or 0
        keep = tracked if tracked in live else live[0]
        stopped: list[int] = []
        failures: list[str] = []
        for pid in [p for p in live if p != keep]:
            why = _stop_pid(pid)
            if why:
                failures.append(f"radar: WARNING — could not stop duplicate poller "
                                f"PID {pid} on {source}:{watcher_id}: {why}. It is "
                                f"still emitting; stop it with "
                                f"`unwatch:{source}:{watcher_id}`.")
            else:
                stopped.append(pid)
        if stopped:
            lines.append(
                f"radar: reaped {len(stopped)} duplicate poller(s) on "
                f"{source}:{watcher_id} — stopped "
                + ", ".join(str(p) for p in stopped)
                + f"; PID {keep} still polls it"
                + ("" if keep == tracked
                   else f" ({tracked_refusal or 'no pid file named one'})") + ".")
        lines.extend(failures)
    return lines


def start_poller(source: str, watcher_id: str, only: list[str]) -> tuple[str, int]:





















    owner = transport.claim_pidfile(source, watcher_id)
    if owner == transport.CLAIM_UNKNOWN:
        return "unclaimable", 0
    if owner:
        return "alive", owner
    try:
        pid = _spawn_poller(source, watcher_id, only)
    except OSError:
        pid = 0
    if not pid:
        transport.release_pidfile(source, watcher_id, os.getpid())
        return "failed", 0
    transport.record_pid(source, watcher_id, pid)
    return "spawned", pid


def _spawn_poller(source: str, watcher_id: str, only: list[str]) -> int:

    r, w = os.pipe()
    pid = os.fork()
    if pid != 0:

        os.close(w)
        try:
            grand_pid = int(os.read(r, 32).decode().strip() or "0")
        except (OSError, ValueError):
            grand_pid = 0
        os.close(r)
        os.waitpid(pid, 0)
        return grand_pid

    os.close(r)
    os.setsid()
    pid2 = os.fork()
    if pid2 != 0:
        os.write(w, str(pid2).encode())
        os.close(w)
        os._exit(0)

    os.close(w)
    _silence_stdio()
    _exec_labelled(source, watcher_id, only)
    _run_poll_loop(source, watcher_id, only)
    os._exit(0)
    return 0  


def _exec_labelled(source: str, watcher_id: str, only: list[str]) -> None:





















    if not sys.executable:
        return
    try:
        os.execve(sys.executable,
                  transport.poller_argv(source, watcher_id, only),
                  transport.poller_env())
    except OSError:
        return


def _silence_stdio() -> None:

    try:
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        if devnull > 2:
            os.close(devnull)
    except OSError:
        pass























MAX_CONSECUTIVE_POLL_FAILURES = 120









GAVE_UP_EVENT = "watcher_gave_up"















RELOAD_SIGNAL = getattr(signal, "SIGHUP", None)






_RELOAD_FLAG: dict[str, bool] = {"reload": False}


def _handle_reload_signal(*_a: object) -> None:
    _RELOAD_FLAG["reload"] = True










RELOAD_EVENT = "watcher_reloaded"
RELOAD_FAILED_EVENT = "watcher_reload_failed"


def _reload_poller(source: str, watcher_id: str, current: Any) -> Any:













    try:
        reloaded = _load_source(source)
    except Exception as e:  
        transport.emit_event(source, watcher_id, RELOAD_FAILED_EVENT,
                             {"error": f"{type(e).__name__}: {e}"})
        return current
    if reloaded is None:
        transport.emit_event(source, watcher_id, RELOAD_FAILED_EVENT,
                             {"error": f"source {source!r} no longer resolves -- "
                                       f"see `watch:{source}:{watcher_id}` for "
                                       f"where this searched"})
        return current
    transport.emit_event(source, watcher_id, RELOAD_EVENT, {})






    published = transport.read_state(source, watcher_id)
    reload_fingerprint, reload_fp_why = transport.source_fingerprint()
    published["reloaded_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    published["reloaded_fingerprint"] = reload_fingerprint
    published["reloaded_fingerprint_error"] = reload_fp_why
    transport.write_state(source, watcher_id, published)
    return reloaded










SUPERTOOL_WATCH_INTERVAL_ENV = "SUPERTOOL_WATCH_INTERVAL"




MAX_RETRY_AFTER_SECONDS = 3600


def interval_override() -> int | None:








    raw = os.environ.get("SUPERTOOL_WATCH_INTERVAL")
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _retry_after_seconds(retry_after: Any) -> int | None:










    if not retry_after or not isinstance(retry_after, str):
        return None
    try:
        struct = time.strptime(retry_after, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    remaining = calendar.timegm(struct) - time.time()
    if remaining <= 0:
        return None
    return min(int(remaining) + 1, MAX_RETRY_AFTER_SECONDS)


def _wait_interruptible(seconds: int, stop_flag: dict[str, bool]) -> None:


















    for _ in range(max(0, int(seconds))):
        if stop_flag["stop"] or _RELOAD_FLAG["reload"]:
            return
        time.sleep(1)


def _record_give_up(source: str, watcher_id: str, failures: int,
                    message: str, repo: str = "") -> None:











    full = transport.read_state(source, watcher_id)
    full["gave_up"] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "after_failures": failures,
        "message": message,
    }
    transport.write_state(source, watcher_id, full)
    transport.emit_event(
        source, watcher_id, GAVE_UP_EVENT,
        {"after_failures": failures, "last_error": message},
        notify_title=f"watch: {source}:{watcher_id} stopped",
        notify_message=(f"{failures} consecutive failed polls. "
                        f"Last error: {message}"),
        repo=repo,
    )


def _run_poll_loop(source: str, watcher_id: str, only: list[str]) -> None:







    _silence_stdio()



    transport.record_pid(source, watcher_id, os.getpid())

    poller = _load_source(source)
    if poller is None:
        transport.release_pidfile(source, watcher_id, os.getpid())
        return







    published = transport.read_state(source, watcher_id)
    published["only"] = list(only)






    fingerprint, fp_why = transport.source_fingerprint()
    published["forked_fingerprint"] = fingerprint
    published["forked_fingerprint_error"] = fp_why









    published.pop("reloaded_at", None)
    published.pop("reloaded_fingerprint", None)
    published.pop("reloaded_fingerprint_error", None)
    transport.write_state(source, watcher_id, published)




    repo = transport.repo_slug()

    state: dict[str, Any] = transport.read_state(source, watcher_id).get("source_state", {}) or {}






    first_tick = _is_bootstrap_state(state)
    ctx = {"source": source, "id": watcher_id, "only": only}
    interval = interval_override() or int(getattr(poller, "INTERVAL", 30))
    stop_flag = {"stop": False}
    reached_terminal = False





    max_failures = int(getattr(poller, "MAX_CONSECUTIVE_FAILURES",
                               MAX_CONSECUTIVE_POLL_FAILURES))
    consecutive_failures = 0

    def _handle_sigterm(*_a):
        stop_flag["stop"] = True

    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)




    _RELOAD_FLAG["reload"] = False
    if RELOAD_SIGNAL is not None:
        signal.signal(RELOAD_SIGNAL, _handle_reload_signal)

    try:
        while not stop_flag["stop"]:
            if _RELOAD_FLAG["reload"]:
                _RELOAD_FLAG["reload"] = False



                poller = _reload_poller(source, watcher_id, poller)
                interval = interval_override() or int(getattr(poller, "INTERVAL", 30))
                max_failures = int(getattr(poller, "MAX_CONSECUTIVE_FAILURES",
                                           MAX_CONSECUTIVE_POLL_FAILURES))
            try:
                events, new_state = poller.poll(state, ctx)
            except Exception as e:  
                consecutive_failures += 1
                full = transport.read_state(source, watcher_id)
                full["last_error"] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                       "message": str(e),
                                       "consecutive": consecutive_failures}
                transport.write_state(source, watcher_id, full)
                if consecutive_failures >= max_failures:




                    _record_give_up(source, watcher_id,
                                    consecutive_failures, str(e), repo=repo)
                    break
                _wait_interruptible(interval, stop_flag)
                continue





            consecutive_failures = 0

            for ev in events:
                if only and ev.get("event") not in only:
                    continue
                transport.emit_event(
                    source, watcher_id,
                    ev.get("event", "unknown"),
                    ev.get("payload", {}),
                    notify_title=ev.get("notify_title"),
                    notify_message=ev.get("notify_message"),
                    first_tick=first_tick,








                    repo=ev.get("repo") or repo,
                )

            full = transport.read_state(source, watcher_id)
            full["source_state"] = new_state
            full.pop("last_error", None)
            transport.write_state(source, watcher_id, full)
            state = new_state



            first_tick = _is_bootstrap_state(new_state)

            if hasattr(poller, "is_terminal") and poller.is_terminal(new_state):
                reached_terminal = True
                break








            sleep_for = interval
            retry_seconds = _retry_after_seconds(
                new_state.get("retry_after") if isinstance(new_state, dict) else None)
            if retry_seconds is not None:
                sleep_for = retry_seconds
            _wait_interruptible(sleep_for, stop_flag)
    finally:



        transport.release_pidfile(source, watcher_id, os.getpid())



        if reached_terminal:
            transport.clear_state(source, watcher_id)


def main(argv: list[str]) -> int:
    use_utf8_stdout()











    _RELOAD_FLAG["reload"] = False
    if len(argv) < 2:
        print("ERROR: usage: dispatcher.py {watch|unwatch|list|poll} [ARG]")
        return 1
    sub = argv[1]
    rest = argv[2:]
    if sub == transport.POLL_SUBOP:





        try:
            source, watcher_id, only = _parse_args(rest)
        except ValueError as e:
            print(f"ERROR: {e}")
            return 1
        _run_poll_loop(source, watcher_id, only)
        return 0
    if sub == "watch":
        return cmd_watch(rest)
    if sub == "unwatch":
        return cmd_unwatch(rest)
    if sub == "list":
        return cmd_list()
    print(f"ERROR: unknown sub-op {sub!r}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
