#!/usr/bin/env python3
"""Dispatcher for the `watch` preset — handles watch/unwatch/watches sub-ops.

Each `watch:SOURCE:ID[:only=...]` invocation forks a poller child for
SOURCE/ID. The child detaches (setsid + double-fork) and runs the source's
`poller.poll()` function in a loop until terminal or until killed.

The dispatcher does NOT contain source-specific logic. It only:
  - Resolves the source's poller module through `sourcepath.find` (#2135)
  - Manages PID files
  - Spawns + kills children
  - Renders the `watches` table

A source plugin lives at `<dir>/<NAME>/poller.py`, where `<dir>` is
`presets/watch/sources/` or any directory on `SUPERTOOL_WATCH_SOURCES_PATH`
(#2135, `presets/watch/sourcepath.py`). It exposes:
  - INTERVAL: int — seconds between polls
  - poll(state: dict, ctx: dict) -> tuple[list[dict], dict]
        returns (events_to_emit, new_state)
  - is_terminal(state: dict) -> bool
        True when the watcher should stop on its own (merged/closed/finished)

Each event is a dict {event: str, payload: dict, notify_title?: str,
notify_message?: str}. The dispatcher passes them to transport.emit_event.
"""
from __future__ import annotations

import importlib.util
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

# Allow importing transport as a sibling module when launched via `python3 dispatcher.py`.
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))  # for _untrusted
from _console import use_utf8_stdout  # noqa: E402  (glyphs on a cp437 console -- #1388)
import _untrusted  # noqa: E402  (the state files are somebody else's text, #1197)
import naming  # noqa: E402  (which knob put the state directory where it is, #1477)
import sourcepath  # noqa: E402  (a source may live outside the plugin, #2135)
import transport  # noqa: E402


def _load_source(name: str, resolved: "sourcepath.Resolved | None" = None):
    """Import the poller module for SOURCE, from wherever it is allowed to live.

    The one door into `sourcepath.find`, and deliberately so: `radar` and
    `tiers/gl_mrs` call this function rather than resolving a directory of their
    own, so all five watch ops search the same path in the same order. A second
    `__file__ / "sources"` anywhere in this preset is the half-configured shape
    of #1309 re-entering by the back door (#2135).
    """
    poller_path, _origin = sourcepath.find(name, resolved)
    if poller_path is None:
        return None
    spec = importlib.util.spec_from_file_location(f"watch_source_{name}", poller_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Keys a poller writes when it could not look at all (#541). A state made only
# of these is bookkeeping about a failed lookup, not an observation of anything.
LOOKUP_ONLY_STATE_KEYS = {"lookup", "error"}


def _is_bootstrap_state(state: dict) -> bool:
    """True when this watcher has never successfully observed the world.

    #464 keys "this emission describes what I found, not what changed" on state
    being empty. Once a failed poll writes `{lookup, error}`, empty is no longer
    the right test: a watcher whose *first* poll 401s has non-empty state and
    has still seen nothing, so the first successful poll after the outage would
    report an already-red MR as a live transition.
    """
    return not state or set(state) <= LOOKUP_ONLY_STATE_KEYS


def _parse_args(parts: list[str]) -> tuple[str, str, list[str]]:
    """Parse SOURCE ID [only=ev1,ev2 ...] from positional argv segments.

    Supertool's {args} placeholder explodes the colon-separated op into one
    argv element per segment, so we receive ["gitlab-mr", "21803", "only=..."]
    rather than a single colon-joined string.

    Returns (source, id, allowed_event_keys_or_empty_meaning_all).
    """
    if len(parts) < 1 or not parts[0]:
        raise ValueError("missing SOURCE")
    if len(parts) < 2 or not parts[1]:
        raise ValueError(f"missing ID for source {parts[0]!r}")
    source, watcher_id = parts[0], parts[1]
    # PID/state filenames use `__` to separate source and id (see transport.py).
    # Allowing it inside either field would make `list_active_pids` ambiguous.
    if "__" in source or "__" in watcher_id:
        raise ValueError("SOURCE and ID must not contain '__' (reserved as filename separator)")
    # Both fields are interpolated straight into a /tmp path. Feed sources take
    # a filter string as their id, so this is now reachable from ordinary use.
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
    # `watch:SOURCE:ID:reload` (#2212) -- a third form beside `only=`, on the
    # SAME op rather than a new one, because it is asking for a poller that
    # already exists to pick up a change, not to be started. Checked before
    # any of the spawn machinery below runs: reload never spawns, and the
    # `sourcepath.resolve()` call just under this would otherwise search and
    # report on a start that is not happening.
    if "reload" in parts[2:]:
        return cmd_reload(source, watcher_id)
    resolved = sourcepath.resolve()
    # The same resolution the refusal below reports on. Resolving twice would
    # stat every entry twice and, worse, let what was loaded and what is
    # reported describe two different moments on disk.
    poller = _load_source(source, resolved)
    if poller is None:
        # Every directory that was consulted, and every one that was declared
        # and could not be (#2135). `Available: <shipped>` named neither the
        # directory those came from nor the one the operator had just
        # configured, so an absence arrived without saying where it looked --
        # in the single message a user hits while setting the feature up.
        print(f"ERROR: unknown source {source!r}. Searched:")
        for line in sourcepath.search_report(resolved):
            print(line)
        return 1
    # After the refusal, not before it: the refusal already prints every
    # directory and every declined entry, and printing both put the same
    # sentence on screen twice on the one path where a reader is reading
    # carefully.
    for line in sourcepath.op_lines("watch", resolved):
        print(f"watch: {line}")
    status, pid = start_poller(source, watcher_id, only)
    if status == "alive":
        # Never silent, and never rendered like a clean start: an operator who
        # cannot tell "started" from "refused" learns nothing from running the
        # op twice, which is how the duplicates in #476 went unnoticed for a
        # day. Say what was found, and which live process holds the slot.
        print(f"Already watching {source}:{watcher_id} (PID {pid}) — "
              f"not starting a second. "
              f"Use ./supertool 'unwatch:{source}:{watcher_id}' to stop it.")
        return 0
    if status == "failed":
        print(f"ERROR: could not spawn a poller for {source}:{watcher_id}")
        return 1
    if status == "unclaimable":
        # The provenance rather than a fixed variable name (#1477): under
        # `SUPERTOOL_WATCH_NAME` the state directory is derived, so naming
        # `SUPERTOOL_WATCH_STATE_DIR` here would send the operator to a knob
        # that is not the one in force. Derived no longer implies that variable
        # is unset — a poller re-exec'd through `poller_env` is handed the
        # derivation in it — and `state_dir_provenance` says which of the two
        # this process is (#1534).
        print(f"ERROR: could not claim the slot for {source}:{watcher_id} — its "
              f"pid file at "
              f"{naming.flat_path(transport.pid_path(source, watcher_id))} could "
              f"not be created. Nothing was started, and nothing here knows "
              f"whether a poller is already running for this id. Check that "
              f"{naming.flat_path(transport.STATE_DIR)} is a writable directory "
              f"({naming.state_dir_provenance(transport.RESOLVED)}).")
        return 1
    # An explicit re-arm is the operator saying they have seen the deaths and
    # are starting over — the one door out of the respawn cap in
    # `transport.DEATH_RESPAWN_LIMIT`. Nothing automatic clears the ledger.
    if transport.clear_deaths(source, watcher_id):
        print(f"Cleared the recorded deaths for {source}:{watcher_id} — "
              f"radar will respawn it again if it dies.")
    print(f"Watching {source}:{watcher_id} (PID {pid})")
    if only:
        print(f"Filter: {','.join(only)}")
    print(f"State: {transport.state_path(source, watcher_id)}")
    return 0


def cmd_reload(source: str, watcher_id: str) -> int:
    """`watch:SOURCE:ID:reload` -- signal a running poller to re-import its
    own `poller.py` in place, keeping its baseline (#2212).

    `unwatch` + `watch` is the alternative and it works, but it forks a fresh
    process with an empty `state`: the first tick after that re-announces
    everything the old process already knew about as new. For a fleet with
    many watched entities that is minutes of baseline noise to deploy a
    one-line fix, and the announcements are not merely slow, they are wrong
    -- nothing actually changed on the box.

    A multi-signal, on the same evidence `cmd_unwatch` requires before it
    multi-kills: every PID here comes from a process whose own argv names
    this exact source and id **and this channel** (`transport.watcher_pids`).
    An untracked survivor on this slot is signalled too, for the same reason
    `unwatch` stops one -- a poller this channel cannot see is one it cannot
    tell has picked up the fix either.
    """
    for line in sourcepath.op_lines("watch"):
        print(f"watch: {line}")
    if RELOAD_SIGNAL is None:
        print("ERROR: this platform has no SIGHUP, so a poller cannot be "
              "signalled to reload in place. "
              f"./supertool 'unwatch:{source}:{watcher_id}' then "
              f"./supertool 'watch:{source}:{watcher_id}' is the only path "
              f"here, and it loses the baseline -- the whole reason this op "
              f"exists.")
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
        print(f"Use ./supertool 'watch:{source}:{watcher_id}' to start one.")
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
    if not info["scan_ok"]:
        print("Process scan unavailable -- only the tracked PID was "
              "considered, so an untracked poller for this id would not "
              "have been signalled.")
    return 1 if failures else 0


def _stop_pid(pid: int) -> str:
    """SIGTERM, then SIGKILL. Empty string on success, else why not.

    Never raises. One PID this process may not signal must not abort a set, and
    an OSError escaping from a process that had already exited on its own would
    end the op with survivors still running — which is #511 with extra steps.
    """
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
    """Foreign pollers on exactly this slot — the #1893 disclosure.

    `_foreign_poller_lines` answers "what does this board not show at all",
    which is a fleet-wide question. This one answers "is *this* slot covered
    by somebody else's poller", which is the question an operator staring at
    an `unwatch` that reached zero of this channel's pollers actually has.
    Same rule as #1881/#1890: this channel may not act on what it finds here,
    so it is stated, never offered — no PID is printed, because a printed PID
    reads as a target.

    [] when the scan did not run (nothing here is evidence of absence) or when
    it ran and found nothing on this slot on another channel.
    """
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
    # Not "on another channel" unconditionally: an `unknown` entry is a
    # poller whose argv predates the channel token, and its true channel is
    # not established -- it could in fact be this one's, wearing a stale
    # label. Categorical wording here would assert more than the census
    # knows, which is exactly the shape #1881 was filed against one layer up.
    header = (f"The process scan also saw poller(s) for {source}:{watcher_id} "
              + ("on another channel, " if other_lines else "whose channel "
                 "could not be established, ")
              + "unaffected by this unwatch:")
    return ([header] + lines
            + ["To act on those, run `unwatch` under the SUPERTOOL_WATCH_NAME "
               "that derives their state dir."])


def _disclose_or_decline_foreign(census: dict[str, Any], source: str,
                                  watcher_id: str) -> None:
    """`_foreign_slot_lines`, plus the third state its own `[]` return hides.

    `[] ` out of `_foreign_slot_lines` means two different things — the scan
    ran and found nothing on this slot elsewhere, or the scan never ran at
    all — and only the caller holds `census["scan_ok"]` to tell them apart.
    Printing nothing in both cases is the absence-read-as-absence defect this
    whole issue is about, one call deeper: a caller that reached one of the
    two early "nothing stopped" branches below with a broken scan got no
    signal that a foreign poller could not be ruled out, where the plain
    "no PID file, no process" branch already said so.
    """
    if not census["scan_ok"]:
        print("The process scan for other channels was unavailable, so a "
              "poller covering this slot on another channel could not be "
              "ruled out.")
        return
    for line in _foreign_slot_lines(census, source, watcher_id):
        print(line)


def _report_nothing_stopped(source: str, watcher_id: str, info: dict[str, Any],
                             census: dict[str, Any]) -> None:
    """Say which kind of nothing this is. There are three, and they differ."""
    if info.get("tracked_refusal"):
        # Not "no PID file": there is a name here and this process would not
        # follow it. The two send an operator to different places, and the
        # second one means somebody planted it (#1200).
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
    """Stop every live poller for SOURCE:ID and name each one.

    A multi-kill, deliberately. The one-PID model failed the other way in #511:
    `unwatch` stopped the tracked poller, the untracked ones kept emitting into
    a context window, and the next `unwatch` answered "No active watcher" while
    the state file was still being rewritten every tick. The only recovery was
    `pkill`. A survivor nobody can reach is worse than a stop that is broader
    than one process, *provided* the operator can see what it did.

    So the breadth is bounded by evidence, not by a guess: every PID here comes
    from a process whose own argv names this exact source and id as whole
    tokens **and names this channel** (see `transport.poller_argv`), each is
    printed with its provenance before any signal is sent, one that will not
    die is named rather than swallowed, and an absence is only reported as an
    absence when the scan that would have found a survivor actually ran.

    The channel token is #1514: without it this stopped a poller belonging to
    another channel, offered by a board that had listed it as this channel's
    own orphan.

    Not reached, and it matters: a poller spawned before the labelling landed
    still wears its parent's argv, so it cannot be told apart from the process
    that forked it — and since #1514 the same is true of one whose argv names
    no channel, which is any poller started before that token existed.
    `pkill -f presets/watch/` remains the only way to clear those, once. See
    docs/presets/watch.md.
    """
    try:
        source, watcher_id, _ = _parse_args(parts)
    except ValueError as e:
        print(f"ERROR: {e}")
        return 1
    # Where sources come from, on this op too (#2135). `unwatch` loads no
    # poller, so this changes nothing it does -- and that is the point: a
    # `watch_sources_path` declared for `watch` alone is a fleet one op can
    # start and another cannot account for, and the op that says nothing is the
    # one where the operator never finds out.
    for line in sourcepath.op_lines("unwatch"):
        print(f"unwatch: {line}")
    # One `ps` for both: `watcher_pids` needs only the `mine` bucket to decide
    # what it may act on, and the #1893 disclosure below needs the other two.
    # `poller_census` is what `scan_poller_pids` already computes internally,
    # so threading it through costs nothing extra.
    census = transport.poller_census()
    info = transport.watcher_pids(
        source, watcher_id, scan=(census["mine"], census["scan_ok"]))
    pids = [pid for pid in info["pids"] if pid > 1 and pid != os.getpid()]
    skipped = [pid for pid in info["pids"] if pid not in pids]
    if skipped:
        print("Not signalling " + ", ".join(str(p) for p in skipped)
              + " — a watcher is never PID 1 nor this process.")
    # An unreadable pid file is not released. `release_pidfile` with no `pid`
    # unlinks unconditionally — that is correct for a poller giving up its own
    # slot, and wrong here: this arm has just told the operator to inspect the
    # path, and removing it is removing the thing to inspect. It also puts the
    # unlink back on the one path #1200 took it off everywhere else.
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
    """`unwatch` is the operator saying "seen". Clear the supervision record.

    Without this every deliberate stop would leave a permanent warning row on
    the board, readers would learn to skim it, and skimming is how a real red
    gets missed — the failure #511 opens with. A deliberate stop is not a loss
    of coverage, it is coverage being withdrawn on purpose.
    """
    if transport.clear_deaths(source, watcher_id):
        print(f"Acknowledged the recorded death(s) for {source}:{watcher_id}.")


def _row_note(row: dict[str, Any]) -> str:
    notes = []
    if row.get("state_refusal"):
        # First, because every other note on this row is a statement about a
        # file that was read, and this row's was not (#1197). An empty
        # LAST_EVENT here means "I could not look", and a board that renders
        # that identically to "nothing has happened" is the defect this repo
        # keeps filing.
        notes.append(f"state unread — {row['state_refusal']}")
    if row.get("dead"):
        recorded = row.get("deaths") or []
        last = recorded[-1].get("pid") if recorded else "?"
        notes.append(f"LOST — PID {last} died, no poller since"
                     + (f" ({len(recorded)} deaths recorded)" if len(recorded) > 1 else ""))
    elif len(row.get("deaths") or []) > 1:
        # A slot that died once and healed cleanly says so on the radar run
        # that healed it and then goes quiet. Only a *flapping* one keeps a
        # note here — a permanent mark on a covered slot is what teaches a
        # reader to skim the board, and the board has exactly one job.
        notes.append(f"flapping — {len(row['deaths'])} deaths recorded, currently respawned")
    if row.get("orphan"):
        notes.append("no pidfile")
    if row.get("extra"):
        notes.append(f"{len(row['pids'])} live pollers: "
                     + ", ".join(str(p) for p in row["pids"]))
    return "; ".join(notes)


def _scan_unavailable_reason() -> str:
    """Which kind of unavailable this is — the platform's, or this run's.

    `watches` is the surface where someone is asking about the fleet on
    purpose, so it is where the permanent version belongs. radar's board
    carries only the one that is news (see `reap_duplicate_pollers`).
    """
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
    """What the scan saw that this board may not act on. [] when there is none.

    #1881: 564 orphaned pollers on one other channel, `watches` printing `No
    active watchers. None recorded as lost either.`, and an operator whose only
    remaining tool was the `pkill` this preset tells them not to use. The scan
    had seen all 564 and `scan_poller_pids` dropped them, which is right for
    every caller that *acts* — the reap on that set is a cross-channel kill
    (#1514) — and wrong for the one that only speaks.

    So: counts, never rows. No SOURCE/ID is named here, because naming one is
    what invites `unwatch:SOURCE:ID` against a slot this channel does not own,
    and that offer is the exact render #1514 was filed to remove. The route out
    is the other channel's own board, and the state directory is printed so the
    operator can get there.

    Empty when the scan did not run: a disclosure reading `0 pollers on another
    channel` off a scan that never happened is this issue one layer in. The
    caller prints `_scan_unavailable_reason()` for that case instead.
    """
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
    for channel, slots in sorted(other.items()):
        count = sum(len(pids) for pids in slots.values())
        if channel in dirs:
            where = f"state dir {naming.flat_path(dirs[channel])}"
        elif dir_state != transport.STATE_DIR_OK:
            # Not "no directory matches" — nobody could look. The two answers
            # send an operator to different places.
            where = f"could not be resolved to a directory ({dir_why})"
        else:
            where = (f"no state directory under "
                     f"{naming.flat_path(naming.BASE_DIR)} hashes to it")
        out.append(f"  {count} on channel {_untrusted.flat(channel)}, {len(slots)} slot(s) — {where}")
    if unknown:
        count = sum(len(pids) for pids in unknown.values())
        out.append(f"  {count} whose channel cannot be told from their argv "
                   f"(started before the channel token existed), "
                   f"{len(unknown)} slot(s)")
    out.append("`unwatch` here reaches only this channel's slots. To act on "
               "another channel's, run `watches` under the "
               "SUPERTOOL_WATCH_NAME that derives its state dir.")
    return out


def cmd_list() -> int:
    """The authoritative view of the watcher fleet.

    Authoritative because `ps` is not: a poller that predates the argv
    labelling shows its parent's command line, so two watchers on different MRs
    can render as byte-identical rows. In #511 that read as duplicates and cost
    two wrong kills. This table is built from PID files *and* a scan for
    labelled pollers, so it also shows the two things the PID files alone
    cannot: an id with more than one live poller, and a poller whose PID file
    was deleted out from under it.
    """
    # Above the board, because the board is a board *of a channel* and until
    # #1495 it printed neither the name nor the export overriding it. Empty on
    # the default paths with no override — a banner on every board is one nobody
    # reads. One accessor, so this and `radar` cannot disagree.
    for line in transport.channel_disclosure():
        print(f"watches: {line}")
    for line in sourcepath.op_lines("watches"):
        print(f"watches: {line}")
    census = transport.poller_census()
    rows, scan_ok = transport.list_watchers(census)
    # Above every early return below, because a fleet running on another channel
    # is news whether or not this one has rows — and the arms that return early
    # are precisely the ones #1881 was filed against. One `ps` feeds both.
    foreign = _foreign_poller_lines(census)
    for line in foreign:
        print(f"watches: {line}")
    dir_state, dir_why = transport.state_dir_status()
    if dir_state == transport.STATE_DIR_UNREADABLE:
        # Printed whether or not there are rows: the pid files are the primary
        # population and this board is built from a listing that did not happen,
        # so neither an empty board nor a short one is evidence of absence.
        print(f"WARNING — {dir_why}, so the poller slots recorded there could "
              f"not be enumerated. This board is built from what the process "
              f"scan found and nothing else; it is not evidence of absence.")
    if not rows:
        if dir_state == transport.STATE_DIR_ABSENT:
            # A knowable state rather than a failure, and the crash it replaces
            # took down every other op in the same call (#1502). Nothing is
            # created here: only a spawn creates a derived state directory, and
            # an operator-supplied one is never manufactured at all (#693).
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
            # The WARNING above is the whole answer about the directory. `No
            # active watchers` would be a claim about the fleet made on the
            # strength of a listing that never ran — but the process scan is a
            # *second*, independent gap, and reporting one of two blindnesses is
            # how a board starts lying quietly. Both arms disclose it.
            if not scan_ok:
                print(_scan_unavailable_reason())
            return 0
        if not scan_ok:
            print("No watchers by PID file — and the process scan was "
                  "unavailable, so an untracked poller could not be ruled out.")
            print(_scan_unavailable_reason())
            return 0
        if foreign:
            # The unqualified sentence is a claim about the *fleet*, and the
            # lines just above it counted pollers that are part of one. Printing
            # both is how #1881's board managed to disclose 564 processes and
            # deny them in consecutive lines. Two sentences, each true of what
            # it is about: this one is scoped, and the clean case below keeps
            # the strong wording it has earned.
            print("No watchers on this channel. None recorded as lost either.")
            return 0
        print("No active watchers. None recorded as lost either.")
        return 0
    for r in rows:
        r["_pid"] = ("-" if r.get("dead") else
                     str(r["pid"]) + (f" (+{len(r['extra'])})" if r["extra"] else ""))
        # Flattened here rather than in the rows, and rather than in
        # `transport.read_state` (#1197). `source` and `id` are the rows'
        # identity — `list_watchers` matches them against the process scan's
        # keys — so mutating them upstream would change which slots the board
        # believes are covered. `last_event` comes out of a state file that is
        # read, mutated and written back six times over, so flattening at the
        # read would put the mangled form on disk. This is the render, it is
        # the only place these three become text, and it is where they stop
        # being anybody else's words.
        #
        # Every one of them is somebody else's: `source` and `id` are parsed
        # out of a *filename* in a world-writable directory, and a POSIX
        # filename carries any byte but `/` and NUL. Before this, a state file
        # whose `last_event` held two newlines printed a whole extra row —
        # a plausible MR, watched, green — onto a fixed-width table.
        #
        # Before the widths, not after: `len()` of an unflattened value sizes
        # the column against a string that will never be printed.
        r["_source"] = _untrusted.flat(r["source"])
        r["_id"] = _untrusted.flat(r["id"])
        r["_last_event"] = _untrusted.flat(r["last_event"] or "-")
        r["_note"] = _untrusted.flat(_row_note(r))
        r["_started"] = _untrusted.flat(r["started"] or "-")
        # #1183: the column that stops a stranded fleet rendering as a quiet
        # one. Four fixed labels out of `transport.delivery_of`, which reads
        # `last_emit` and nothing else — so this board, `radar` and
        # `channel:health` cannot disagree about the same field. The value is
        # this repo's own vocabulary rather than anybody else's text, which is
        # why it is not in the `_untrusted` note above.
        r["_delivery_state"] = transport.delivery_of(
            r.get("last_emit"), r.get("state_refusal") or "")
        r["_delivery"] = transport.DELIVERY_LABELS[r["_delivery_state"]]
        # #2179: does this poller's own recorded fork-time source match what
        # is on disk right now. Computed per row rather than once for the
        # whole table because a dead row's `state_refusal` (or a pre-#2179
        # row's absent fingerprint) has to reach `VERSION_UNKNOWN` the same
        # way `_delivery_state` does above — a row this board could not read
        # must not borrow another row's verdict.
        r["_version_state"], r["_version_why"] = transport.version_state_of(
            r.get("forked_fingerprint"), r.get("forked_fingerprint_error"))
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
    # Above the table, not below it: the reader this protects is the one who
    # acts on the first thing they read, which is the same reason
    # `channel._health_note` sits above the stamps it is about. Unconditional
    # once there are rows, because those three columns are always somebody
    # else's words — a note printed only when a value turns out to be hostile
    # would be a claim about the render rather than about the source.
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
    # #1183. Two separate paragraphs because they are two separate facts, and
    # collapsing them would be the trade this fix exists to refuse: NO LISTENER
    # is a definite negative about delivery, `unknown` is the admission that
    # nothing was established. Neither is a verdict about the *poller* — this
    # board never proposes stopping, restarting or reaping one on the strength
    # of the DELIVERY column, and says so, because a render that invited that
    # reading is what cost two live watchers in #511.
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
    # #2179: a poller running old code is not an error, it is old correct
    # behaviour, so it produces well-formed events that are simply wrong and
    # nothing else on this board can notice. Named here rather than left to
    # the VERSION column alone, because a column is easy to skim past and
    # this is exactly the render #2179 was filed against.
    stale = [r for r in rows if r["_version_state"] == transport.VERSION_STALE]
    if stale:
        print()
        print(f"{len(stale)} row(s) above are marked STALE in VERSION: this "
              f"poller forked before the source under presets/watch/ last "
              f"changed, so it is running code a later fix may have replaced. "
              f"Restart it with `unwatch:SOURCE:ID` then `watch:SOURCE:ID` to "
              f"pick up the current source; nothing here restarts it "
              f"automatically.")
        for r in stale:
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
    # Gated on the multi-poller notes specifically, not on the NOTE column: a
    # board whose only note is a LOST row would otherwise be told to go looking
    # for a duplicate poller that is not there.
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
    """Stop every surplus poller on a slot that has more than one. Report it.

    Returns the lines to print: one per slot reaped, plus a WARNING for any PID
    that would not stop. An empty list means the scan ran and found no slot with
    two pollers on it. A scan that could not run returns a `skipped` line and
    kills nothing — see below.

    What this may act on, and why that bound is where it is
    ------------------------------------------------------

    Only PIDs from `transport.scan_poller_pids`, which reads a process's *own*
    argv (#511's `exec` labelling) and takes `source` and `watcher_id` as whole
    tokens. That is the only thing a PID here proves about itself, and it is
    exactly enough for the one judgement this makes: two pollers naming the same
    slot are duplicates of each other, so stopping all but one provably leaves
    the slot covered. No PID is ever killed for being unrecognised, for being
    absent from a pidfile, or for belonging to a slot nobody asked about — the
    #511 damage was three `ps` rows *inferred* to be duplicates, and two of them
    were the watchers for two different MRs.

    So three populations are deliberately not touched:

      * A slot with one poller, tracked or not. A lone orphan is still the only
        thing polling its slot; killing it trades a duplicate nobody has for a
        blind spot, which is the trade #513 says is the wrong way round.
      * A poller spawned before the labelling landed. It wears its parent's
        argv, the scan cannot see it, and nothing can tell it from the process
        that forked it. `pkill -f 'presets/watch/'` once, as docs/presets/watch.md
        says — that judgement is an operator's, not this function's.
      * A poller on another channel, or one whose argv predates the `chan=`
        token and so names no channel at all (#1514). `scan_poller_pids`
        returns neither, and that is where the bound lives rather than here.
        Two channels each running one poller for the same `(source, id)` are
        two slots — two pid files, in two state directories — and grouping
        them as one made the reap stop the poller this channel's pid file did
        not name. A cross-channel kill, reached through a listing bug.
      * Anything at all, when `ps` could not be read.

    The survivor is the pidfile's PID when it is one of the live ones, so the
    slot keeps the poller `watches` and `unwatch` already name; otherwise the
    lowest PID, which is arbitrary but deterministic — the pollers on one slot
    are interchangeable, the choice being *stable* across runs is not.

    Three states, not two
    ---------------------

    `ok` is silent, a finding names every PID it stopped, and a scan that could
    not run says `skipped` out loud. A reaper that cannot see the fleet and
    prints nothing renders byte-identically to one that looked and found it
    clean — which is this repository's recurring defect with a body count
    attached (docs/validators.md, "Declining instead of guessing").

    One PID per signal, never a batch: a batched `kill $PID_LIST` against these
    processes silently no-ops — exit 0, every process still alive, `-9`
    included — while looking exactly like a reap that worked.
    """
    found, scan_ok = transport.scan_poller_pids()
    if not scan_ok:
        if not transport.ps_scan_supported():
            # A platform with no `ps` fails this scan on every run, forever. A
            # line that prints unconditionally is not disclosure, it is
            # furniture: a reader learns to skim it, and then it cannot do its
            # job on the machine where `ps` was there and genuinely did not
            # answer. The absence is permanent, so it is stated where someone
            # asks about the fleet on purpose — `watches`, and the docs — and
            # not on every board. Nothing is hidden that was ever knowable
            # here: no scan means no duplicate was ever visible on this
            # machine, with or without this line.
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
    """Claim the (source, id) slot, then spawn its poller.

    ("alive"|"spawned"|"failed"|"unclaimable", pid). `unclaimable` is the third
    state (#693): the claim did not settle, so this process neither owns the
    slot nor knows who does, and forking on that would be a spawn decided by an
    absence of information.

    The one door to a new poller, for every tier — `watch` and radar's feed
    both come through here, because two spawn sites with two copies of the
    "is one already running?" question is how they came to disagree (#476).

    Ordering is #451's and is load-bearing: the slot is claimed *before* the
    fork, so losing the race costs nothing — no detached child to reap, no
    pidfile to unwind, nothing to clean up. The claim is written with this
    process's PID and only repointed at the grandchild once that PID is known,
    so the slot is never momentarily ownerless.

    A spawn that fails gives the slot back. A claim left behind by a poller
    that never started would refuse every future start for that id, and a
    refusal nobody asked for renders as a watcher quietly not existing.
    """
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
    """Detach a poller child (double-fork) and return its PID to the parent."""
    r, w = os.pipe()
    pid = os.fork()
    if pid != 0:
        # Parent: wait for child to report grandchild PID, then return.
        os.close(w)
        try:
            grand_pid = int(os.read(r, 32).decode().strip() or "0")
        except (OSError, ValueError):
            grand_pid = 0
        os.close(r)
        os.waitpid(pid, 0)
        return grand_pid
    # First child: detach session, fork grandchild, report PID, exit.
    os.close(r)
    os.setsid()
    pid2 = os.fork()
    if pid2 != 0:
        os.write(w, str(pid2).encode())
        os.close(w)
        os._exit(0)
    # Grandchild: close inherited fd, take an argv that names this watcher, run.
    os.close(w)
    _silence_stdio()
    _exec_labelled(source, watcher_id, only)
    _run_poll_loop(source, watcher_id, only)
    os._exit(0)
    return 0  # unreachable


def _exec_labelled(source: str, watcher_id: str, only: list[str]) -> None:
    """Replace this process image with one whose argv names this watcher.

    A poller is forked, so until this call it wears the argv of whatever
    spawned it — radar's, or the feed's. #511 is the bill for that: three `ps`
    rows with byte-identical arguments were read as duplicate feed pollers and
    two were killed, and they were the watchers for two different MRs, one of
    them the MR that most needed watching.

    exec, not `setproctitle`: no new dependency, and the command line it
    produces is not a label *describing* the process, it **is** the process —
    the same argv `transport.poller_argv` matches on, so what `ps` shows and
    what `watches` shows cannot drift apart. The PID is unchanged by exec, so
    the slot claimed before the fork and the PID already reported up the pipe
    both stay correct, and #484's claim-before-fork ordering is untouched.

    STATE_DIR travels in the environment because a fork inherits it and an exec
    does not.

    Returns on failure rather than raising: an unlabelled poller is a working
    poller that is hard to see, which beats no poller at all.
    """
    if not sys.executable:
        return
    try:
        os.execve(sys.executable,
                  transport.poller_argv(source, watcher_id, only),
                  transport.poller_env())
    except OSError:
        return


def _silence_stdio() -> None:
    """Point stdio at /dev/null so a poller cannot write to its parent's terminal."""
    try:
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        if devnull > 2:
            os.close(devnull)
    except OSError:
        pass


#: How many polls in a row may raise before a poller hands its slot back.
#:
#: Generous on purpose, and finite on purpose. The failure this bounds is not a
#: blip -- it is an expired token, a deleted MR, a renamed project, a source
#: module that no longer imports. Every one of those fails identically forever,
#: and before #1852 the loop retried them until a reboot: 22 pollers alive at
#: once on one machine, the oldest eight days into watching an MR that had
#: stopped being interesting.
#:
#: A count rather than a wall-clock age, because a count is what the loop can
#: observe without a clock it would then have to trust across a suspend. At the
#: default 30s interval this is an hour of *uninterrupted* failure, which
#: outlasts a VPN reconnect, a runner restart and a GitLab maintenance window
#: and does not outlast a credential that is gone.
#:
#: A source that knows its own failure modes better overrides it by exposing
#: `MAX_CONSECUTIVE_FAILURES`. Deliberately not an environment variable: the
#: poller is reached through a fork and an exec, so an env var read here is one
#: an operator has to have set in whatever shell spawned radar, and no source
#: has asked for the knob. The per-source attribute is the narrower answer and
#: is where the knowledge actually lives.
MAX_CONSECUTIVE_POLL_FAILURES = 120

#: What a poller says on its way out when the bound above is reached.
#:
#: The dispatcher's event, not a source's, and it is deliberately in no
#: `events.json`: those files declare what a source can emit and what `only=`
#: can select, and neither is true of this key. It bypasses `only` for the same
#: reason -- a watcher that filtered away its own obituary would stop, release
#: its slot and tell nobody, which is the silence this issue is about, one layer
#: further in.
GAVE_UP_EVENT = "watcher_gave_up"

#: `unwatch` + `watch` picks up a merged `poller.py` change, but it forks a
#: fresh process with empty `state` -- `seen` is false again, so the very
#: first tick re-announces everything the old process already knew about as
#: new (#2212). A signal reloads the SAME process's module in place instead,
#: so `state`, which lives in that process's own memory and nowhere this
#: dispatcher can reach or touch, is never replaced.
#:
#: `None` on a platform with no SIGHUP (there is no fork/setsid poller model
#: on such a platform either, so this never needs a second story). Not an
#: env-configurable choice: this is a signal number, not a preset knob, and
#: `getattr(signal, "SIGKILL", signal.SIGTERM)` just above is the same
#: platform-optional idiom.
RELOAD_SIGNAL = getattr(signal, "SIGHUP", None)

#: Set from the SIGHUP handler below; consulted once per tick by
#: `_run_poll_loop`, never from inside a signal handler itself beyond the
#: single flag write (#2212's whole reason: the loop's own `state`, kept
#: outside this dict entirely, must never be touched from a handler that can
#: interrupt an arbitrary line of Python).
_RELOAD_FLAG: dict[str, bool] = {"reload": False}


def _handle_reload_signal(*_a: object) -> None:
    _RELOAD_FLAG["reload"] = True


#: The dispatcher's own events, in no source's `events.json` and bypassing
#: `only` for the same reason `GAVE_UP_EVENT` does above: a reload that
#: silently kept running old code, or one that quietly picked up nothing to
#: change, would both look like nothing at all -- and #2212 names exactly
#: that silence as the automatic-reload alternative's own hazard. This
#: signal-driven shape is explicit rather than automatic, but a broken edit
#: is exactly as broken either way, so it inherits the same duty: report,
#: never crash the watcher that was trying to pick up a fix.
RELOAD_EVENT = "watcher_reloaded"
RELOAD_FAILED_EVENT = "watcher_reload_failed"


def _reload_poller(source: str, watcher_id: str, current: Any) -> Any:
    """Re-import SOURCE's poller.py in place. Returns the new module, or
    `current` UNCHANGED on any failure.

    Two failure shapes, one outcome: an import that raises (a genuinely
    broken edit -- `_load_source` does not catch `exec_module`'s own
    exceptions) and a source that no longer resolves at all (`_load_source`
    returning None, e.g. the search path changed under it). Either way the
    watcher keeps polling with the code it already had rather than dying on
    its next tick, and exactly one event says which happened -- silence here
    is indistinguishable from a reload that had nothing to pick up, which is
    the same absence-read-as-presence shape this whole preset exists to
    remove.
    """
    try:
        reloaded = _load_source(source)
    except Exception as e:  # noqa: BLE001 — a broken edit must not end the watcher
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
    return reloaded


def _wait_interruptible(seconds: int, stop_flag: dict[str, bool]) -> None:
    """Wait `seconds`, in one-second steps, giving up early on a stop.

    One call for both branches of the poll loop, because they disagreed. The
    success branch already stepped a second at a time and checked the flag
    between steps; the error branch was a single `time.sleep(interval)`, and
    PEP 475 resumes an interrupted sleep for its remaining time rather than
    returning early. So `unwatch` was honoured within a second by a poller that
    was working and ignored for up to a full interval by one that was not --
    which is exactly the poller an operator is most likely to be stopping.
    """
    for _ in range(max(0, int(seconds))):
        if stop_flag["stop"]:
            return
        time.sleep(1)


def _record_give_up(source: str, watcher_id: str, failures: int,
                    message: str, repo: str = "") -> None:
    """Write why coverage ended, then say so on the channel.

    The state file is *kept*, unlike a terminal exit, and the two differ for a
    reason. A terminal watcher has nothing left to explain -- the MR merged --
    so its state file is only a way for a consumer that globs them to report a
    merged MR as an active watch. A give-up is the opposite: `last_error` and
    the count below are the entire record of why the slot went quiet, and
    without them the board can render a stopped watcher but not a re-armable
    one. `gave_up` is its own key rather than an inference from `last_error`,
    because a poller that is failing and still trying writes `last_error` too.
    """
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
    """The poll loop. Writes PID file, runs until terminal or signal.

    Two entry points, both the same process: the grandchild falls through to it
    when the labelling exec could not run, and the exec'd image reaches it via
    the `poll` sub-op in `main`. Nothing here may assume anything inherited
    through the fork, because after an exec nothing was.
    """
    _silence_stdio()

    # The slot was already claimed by the caller in start_poller(); this only
    # repoints it at the PID that is actually going to poll.
    transport.record_pid(source, watcher_id, os.getpid())

    poller = _load_source(source)
    if poller is None:
        transport.release_pidfile(source, watcher_id, os.getpid())
        return

    # Publish the event filter next to the state. `only` decides which of the
    # source's events this poller will ever emit, and it otherwise lived only
    # in this process's memory — so another tier could see the poller was alive
    # and still have no way to tell one that will announce a merge from one
    # filtered away from saying so. `gitlab-mr-feed` asks exactly that (#434),
    # in a place where guessing wrong means a transition nobody reports.
    published = transport.read_state(source, watcher_id)
    published["only"] = list(only)
    # A long-lived poller runs the code it was forked with, and nothing said
    # which version that was (#2179): the fix landed in `8e9ac260`, five days
    # before a still-running poller's fork, was released, and that poller
    # never ran it. Recorded once, here, next to `only` — this is the one
    # moment a poller's own source is *this* source rather than whatever a
    # future `watches` render happens to find on disk.
    fingerprint, fp_why = transport.source_fingerprint()
    published["forked_fingerprint"] = fingerprint
    published["forked_fingerprint_error"] = fp_why
    transport.write_state(source, watcher_id, published)

    # Read once per process, not once per poll (#1952): the cwd's own remote
    # cannot change under a running watcher, and re-shelling out to `git` on
    # every 30s tick would pay for an answer that never differs.
    repo = transport.repo_slug()

    state: dict[str, Any] = transport.read_state(source, watcher_id).get("source_state", {}) or {}
    # No prior state means this watcher knows nothing yet, so whatever its
    # first poll emits describes what it found rather than what changed. That
    # emission is deliberate — it is how a fresh watcher reports an already-red
    # MR, and gitlab-mr-feed leans on it — but the consumer has to be able to
    # tell it apart from a live transition (#464). Keyed on state, not on
    # process age: a poller restarted with its state intact is not bootstrapping.
    first_tick = _is_bootstrap_state(state)
    ctx = {"source": source, "id": watcher_id, "only": only}
    interval = int(getattr(poller, "INTERVAL", 30))
    stop_flag = {"stop": False}
    reached_terminal = False
    # The error branch's own bound (#1852). A failed poll produces no new state,
    # so the terminal check below cannot be consulted from it — which is exactly
    # why it retried forever. Counted here rather than inferred from the state
    # file: the count has to reset on the first success, and a file another
    # process may rewrite is not where a loop invariant belongs.
    max_failures = int(getattr(poller, "MAX_CONSECUTIVE_FAILURES",
                               MAX_CONSECUTIVE_POLL_FAILURES))
    consecutive_failures = 0

    def _handle_sigterm(*_a):
        stop_flag["stop"] = True

    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)
    # `_RELOAD_FLAG` is module-level and this process may be a fork that
    # inherited a set flag from before it existed as this watcher (#2212) --
    # cleared here, at the one moment this loop starts owning it, same as
    # `stop_flag` starting False every time.
    _RELOAD_FLAG["reload"] = False
    if RELOAD_SIGNAL is not None:
        signal.signal(RELOAD_SIGNAL, _handle_reload_signal)

    try:
        while not stop_flag["stop"]:
            if _RELOAD_FLAG["reload"]:
                _RELOAD_FLAG["reload"] = False
                # `state` is untouched — it is not a parameter of this call,
                # on purpose (#2212): only which module object `poller` names
                # changes here, never what the loop already knows.
                poller = _reload_poller(source, watcher_id, poller)
                interval = int(getattr(poller, "INTERVAL", 30))
                max_failures = int(getattr(poller, "MAX_CONSECUTIVE_FAILURES",
                                           MAX_CONSECUTIVE_POLL_FAILURES))
            try:
                events, new_state = poller.poll(state, ctx)
            except Exception as e:  # noqa: BLE001 — never crash, log to state
                consecutive_failures += 1
                full = transport.read_state(source, watcher_id)
                full["last_error"] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                       "message": str(e),
                                       "consecutive": consecutive_failures}
                transport.write_state(source, watcher_id, full)
                if consecutive_failures >= max_failures:
                    # Out through the same `finally` a terminal exit takes, so
                    # the pidfile is released and the slot is handed back. Not
                    # `reached_terminal`: that clears the state file, and the
                    # state file is the only record of why this stopped.
                    _record_give_up(source, watcher_id,
                                    consecutive_failures, str(e), repo=repo)
                    break
                _wait_interruptible(interval, stop_flag)
                continue

            # Whatever went wrong is over. The bound is on a *run* of failures,
            # so a flaky forge that answers one poll in ten keeps its watcher —
            # the guard is against a failure that will never clear, not against
            # a failure rate.
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
                    # A source that already resolved its own repository
                    # (gh-branch does, through gh's own base-repo resolution
                    # -- #1963) wins over this process-level, git-config
                    # read: the two can disagree inside a fork checkout, and
                    # the source's answer is the one every call in its own
                    # event actually ran against. Every other source has no
                    # opinion here, so `ev.get("repo")` is None for them and
                    # this falls back to exactly what it did before.
                    repo=ev.get("repo") or repo,
                )

            full = transport.read_state(source, watcher_id)
            full["source_state"] = new_state
            full.pop("last_error", None)
            transport.write_state(source, watcher_id, full)
            state = new_state
            # Not an unconditional False: a cold start whose polls keep failing
            # has produced state but no observation, so it is still bootstrapping
            # and the first poll that succeeds is still describing what it found.
            first_tick = _is_bootstrap_state(new_state)

            if hasattr(poller, "is_terminal") and poller.is_terminal(new_state):
                reached_terminal = True
                break

            _wait_interruptible(interval, stop_flag)
    finally:
        # Only if this process still owns the slot. A poller shutting down
        # slowly, whose slot was meanwhile reclaimed, must not unlink its
        # successor's claim on the way out (#476).
        transport.release_pidfile(source, watcher_id, os.getpid())
        # A terminal watcher leaves no live process, so its state file is not a
        # record of anything current. Kept, it makes consumers that glob the
        # state files report merged MRs as active watches.
        if reached_terminal:
            transport.clear_state(source, watcher_id)


def main(argv: list[str]) -> int:
    use_utf8_stdout()
    if len(argv) < 2:
        print("ERROR: usage: dispatcher.py {watch|unwatch|list|poll} [ARG]")
        return 1
    sub = argv[1]
    rest = argv[2:]
    if sub == transport.POLL_SUBOP:
        # The poller itself, running under the argv `_exec_labelled` gave it.
        # Not an operator-facing sub-op: `watch` spawns, this *is* the spawn.
        # It has to exist and has to keep working — an argv naming a sub-op the
        # dispatcher does not implement would exit every watcher on start, and
        # the fleet would render as a quiet afternoon.
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
