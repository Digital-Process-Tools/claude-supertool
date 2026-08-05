"""Transport writers for watch pollers.

Pollers emit events through three transports:
- UDS socket (NDJSON) at /tmp/supertool-watch.sock — for consumers like the
  Phase 2 channel server. Silent when no listener bound.
- Status file at /tmp/supertool-watch-{source}-{id}.state.json — last-known
  state so `watches` op can render it without scanning processes.
- macOS osascript desktop notification — human-facing ping on terminal/error.
  No-op on non-macOS.

All writers swallow errors. A watcher must never die because a transport
hiccupped.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))  # for _proc

import _proc  # noqa: E402  (the one liveness probe, shared with gl-mrs / gh-prs)

# Overridable (#581): the Phase 2 consumer (channel.ts:35) already reads this
# variable, and four shipped surfaces — the #550 refusal message and three
# lines in notifiers/claude-channel/README.md, one of them a security claim
# about per-user isolation — tell an operator to set it here too. It never
# worked: this was a plain constant. Matches STATE_DIR's `or` idiom below so
# an operator exporting an empty string gets the default, not a connect() to "".
SOCK_PATH = os.environ.get("SUPERTOOL_WATCH_SOCK") or "/tmp/supertool-watch.sock"
# Overridable so a poller re-exec'd under its own argv (see `poller_argv`) keeps
# writing where its parent was writing. Without it, exec would move a test's
# poller from the test's tmp dir to the real /tmp — a fork inherits monkeypatched
# module state, an exec does not.
STATE_DIR = os.environ.get("SUPERTOOL_WATCH_STATE_DIR") or "/tmp"
STATE_DIR_ENV = "SUPERTOOL_WATCH_STATE_DIR"

# The sub-op a poller runs under, and the path that identifies it in `ps`. A
# poller is forked, so without an exec it wears *its parent's* argv: every
# per-MR watcher displays the feed's command line, which in #511 was read as
# three duplicate feed pollers and cost two wrong kills. These two constants are
# the whole of the labelling — everything that identifies a poller from outside
# reads them back.
POLL_SUBOP = "poll"
DISPATCHER_TAIL = "watch/dispatcher.py"

# How many recorded deaths a slot may accumulate before `radar.heal` stops
# respawning it. Healing is right (#417's amendment argues reconcile-and-heal
# over report), but a watcher respawned forever without anyone being told
# converts a visible failure into an invisible loop — which is #513 wearing a
# different hat. The cap is what makes the failure surface instead of looping,
# and the refusal is loud precisely because it is the end of the automation.
DEATH_RESPAWN_LIMIT = 3

# Refuse to follow a pre-existing symlink at the pidfile path (#148's guard, in
# the second place that opens a /tmp path by predictable name). Windows has no
# such flag, and 0 leaves the open otherwise unchanged.
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

# `claim_pidfile` could not answer: the open failed, no file was created, and
# nothing here knows who — if anyone — holds the slot. Distinct from `0` ("it is
# yours, go spawn") and from a PID ("that live process holds it"). It used to
# return `0` from both of the paths below, which is the strongest of the three
# claims made on the evidence of the weakest (#693): the caller then forked a
# poller against a slot it did not own, on a machine where the state directory
# is unwritable or gone — exactly when two pollers are least wanted.
CLAIM_UNKNOWN = -1


def state_path(source: str, watcher_id: str) -> str:
    return f"{STATE_DIR}/supertool-watch-{source}__{watcher_id}.state.json"


def pid_path(source: str, watcher_id: str) -> str:
    return f"{STATE_DIR}/supertool-watch-{source}__{watcher_id}.pid"


def read_pid(source: str, watcher_id: str) -> int:
    """PID recorded for this slot, or 0 when there is no readable file."""
    try:
        raw = Path(pid_path(source, watcher_id)).read_text(encoding="utf-8")
    except OSError:
        return 0
    try:
        return int(raw.strip())
    except ValueError:
        return 0


def claim_pidfile(source: str, watcher_id: str) -> int:
    """Take the (source, id) poller slot, or report the live PID that holds it.

    Returns 0 when this process now owns the slot, the PID of the poller that
    already does, or `CLAIM_UNKNOWN` when the claim could not be settled — an
    `os.open` that failed for anything other than "it already exists", and a
    retry that ran out. Neither of those created a file, so neither may be
    reported as ownership.

    `O_CREAT|O_EXCL` is the atomic part, and it is the whole fix for #476: the
    spawn sites used to *test* the pidfile and then fork, but the pidfile is
    published by the grandchild after a fork, an import and a detach, so every
    caller looking inside that window saw an empty slot and started its own
    poller. That is how nine pollers over one filter accumulate in same-second
    groups. Exactly one process can create the file, so exactly one starts.

    A pidfile whose owner is dead is removed and the claim retried once. The
    opposite failure — a crashed poller wedging its slot shut forever — is
    worse than a duplicate: a duplicate is visible in `watches` and in `ps`,
    while a slot nobody can claim leaves the population unwatched, and an
    unwatched population renders exactly like one with nothing to report.
    """
    for _ in range(2):
        try:
            fd = os.open(pid_path(source, watcher_id),
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                         0o600)
        except FileExistsError:
            existing = read_pid(source, watcher_id)
            if existing and _pid_alive(existing):
                return existing
            if existing:
                # The slot recorded a poller that is gone. Removing the file
                # here is what let a death vanish without a trace: this is the
                # path radar's heal takes, so the evidence had to be written
                # down before the claim erases it (#513).
                record_death(source, watcher_id, existing)
            try:
                os.unlink(pid_path(source, watcher_id))
            except FileNotFoundError:
                pass
            continue
        except OSError:
            # An unwritable or absent state dir, a path that is a directory
            # (IsADirectoryError on POSIX, PermissionError on Windows), a
            # refused symlink. No file exists and no owner was identified.
            return CLAIM_UNKNOWN
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"{os.getpid()}\n")
        return 0
    # Both attempts met a file that existed and had no live owner, and both
    # unlinks lost the follow-up race. Something is creating this pidfile faster
    # than we can take it; that is not this process owning the slot.
    return CLAIM_UNKNOWN


def record_pid(source: str, watcher_id: str, pid: int) -> None:
    """Point an already-claimed slot at the PID actually running the poll loop.

    The claimant writes its own PID first so the slot is never briefly owned by
    nobody; this replaces it with the detached grandchild's once that PID is
    known. Both the claiming parent and the grandchild call it with the same
    value, so the order they arrive in does not matter.
    """
    try:
        Path(pid_path(source, watcher_id)).write_text(f"{pid}\n", encoding="utf-8")
    except OSError:
        pass


def release_pidfile(source: str, watcher_id: str, pid: int | None = None) -> None:
    """Give up the slot. With `pid`, only if that PID still owns it.

    A poller whose slot was reclaimed while it was shutting down must not
    unlink its successor's claim on the way out — that would hand the next
    caller an empty slot and put a second poller back on the same filter.
    """
    if pid is not None and read_pid(source, watcher_id) != pid:
        return
    try:
        os.unlink(pid_path(source, watcher_id))
    except OSError:
        pass


def emit_socket(payload: dict[str, Any]) -> None:
    """Best-effort write of one NDJSON line to the UDS socket. Silent if no listener."""
    if not os.path.exists(SOCK_PATH):
        return
    s: socket.socket | None = None
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect(SOCK_PATH)
        s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
    except OSError:
        return
    finally:
        if s is not None:
            try:
                s.close()
            except OSError:
                pass


def write_state(source: str, watcher_id: str, state: dict[str, Any]) -> None:
    """Atomically replace the status file with the latest known state."""
    path = state_path(source, watcher_id)
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def read_state(source: str, watcher_id: str) -> dict[str, Any]:
    path = state_path(source, watcher_id)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def clear_state(source: str, watcher_id: str) -> bool:
    """Delete the status file. True when a file was actually removed.

    Called when a watcher reaches a terminal state. The poller is gone, so the
    file is not a record of anything live — leaving it behind makes every
    consumer that globs the state files report long-merged MRs as active.
    """
    try:
        os.unlink(state_path(source, watcher_id))
    except OSError:
        return False
    return True


def deaths(source: str, watcher_id: str) -> list[dict[str, Any]]:
    """Every unacknowledged death recorded for this slot, oldest first.

    Kept in the state file rather than beside it, and that placement is the
    design: a poller that reaches a terminal state deletes its state file on
    the way out, so a legitimate exit cannot leave a ledger behind for anyone
    to misread as a loss. The invariant holds by construction rather than by a
    check that could drift out of step with the poll loop.
    """
    recorded = read_state(source, watcher_id).get("deaths")
    return [d for d in recorded if isinstance(d, dict)] if isinstance(recorded, list) else []


def record_death(source: str, watcher_id: str, pid: int) -> bool:
    """Note that `pid` held this slot and is gone. True when newly recorded.

    Idempotent on the PID, because two readers legitimately reap one corpse:
    `watches` prunes stale pid files while rendering, and `claim_pidfile` does
    the same immediately before a heal reuses the slot. Counting one death
    twice would trip the respawn cap early, and a cap that fires without the
    failure having happened is a false red on the one surface that must not
    grow them.
    """
    if not pid:
        return False
    current = read_state(source, watcher_id)
    recorded = current.get("deaths")
    ledger = [d for d in recorded if isinstance(d, dict)] if isinstance(recorded, list) else []
    if any(d.get("pid") == pid for d in ledger):
        return False
    ledger.append({"pid": pid, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    current["deaths"] = ledger
    write_state(source, watcher_id, current)
    return True


def clear_deaths(source: str, watcher_id: str) -> bool:
    """Acknowledge every death on this slot. True when there was one to clear.

    Only ever called from an explicit operator action — `unwatch` (I have seen
    it and I am stopping this watcher) or `watch` (I have seen it and I am
    re-arming it). Nothing automatic clears the ledger, because a respawn that
    silently wiped it would restore the invisible loop the cap exists to stop.
    """
    current = read_state(source, watcher_id)
    if "deaths" not in current:
        return False
    current.pop("deaths", None)
    write_state(source, watcher_id, current)
    return True


def reap_dead_pidfile(source: str, watcher_id: str) -> int:
    """Record the death a stale pid file is evidence of, and clear the file.

    Returns the dead PID, or 0 when the slot is empty or its poller is alive.

    A pid file naming a dead process is the *only* evidence of a death, and it
    is unambiguous: the poll loop releases the slot on a deliberate stop and on
    a terminal exit alike, and `unwatch` releases it too. What is left is a
    poller that never ran its shutdown path — SIGKILL, a crash, an OOM kill, a
    reboot. Deliberately not derived from the process scan: a poller spawned
    before the argv labelling (#512) is invisible to it while being perfectly
    alive, and reporting those as losses would flood the board on the first run
    after this lands.
    """
    pid = read_pid(source, watcher_id)
    if not pid or _pid_alive(pid):
        return 0
    record_death(source, watcher_id, pid)
    release_pidfile(source, watcher_id)
    return pid


def desktop_notify(title: str, message: str) -> None:
    """Fire-and-forget macOS notification. No-op elsewhere."""
    if sys.platform != "darwin":
        return
    if not shutil.which("osascript"):
        return
    # Titles and bodies arrive from remote repos, so they routinely contain
    # quotes, backslashes and other characters that are syntax to AppleScript.
    # Interpolating them into the script text makes the notification depend on
    # someone else's branch name; osascript reads positional arguments after
    # `--` into `argv`, where they are values rather than source.
    try:
        subprocess.run(
            [
                "osascript",
                "-e", "on run argv",
                "-e", "display notification (item 1 of argv) with title (item 2 of argv)",
                "-e", "end run",
                "--", message, title,
            ],
            capture_output=True, timeout=3, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return


def emit_event(
    source: str,
    watcher_id: str,
    event_key: str,
    payload: dict[str, Any],
    *,
    notify_title: str | None = None,
    notify_message: str | None = None,
    first_tick: bool = False,
) -> None:
    """All transports in one call.

    Writes the event to the UDS socket (if any listener), refreshes the
    status file with the latest event, and optionally fires a desktop
    notification when title+message are provided.

    `first_tick` marks an event emitted on a watcher's very first poll: a
    report of the state it *found*, not of a change it *observed*. Both are
    worth emitting — a new watcher announcing an already-red MR is the point —
    but week-old outcomes arriving shaped like news is not (#464). It sits
    beside the envelope keys rather than inside `payload`, which is
    source-defined and locked; see docs/presets/watch.md.
    """
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": source,
        "id": watcher_id,
        "event": event_key,
        "payload": payload,
        "first_tick": bool(first_tick),
    }
    emit_socket(record)
    current = read_state(source, watcher_id)
    current["last_event"] = record
    current.setdefault("first_seen", record["ts"])
    # Not part of the wire payload above (that contract is locked, see
    # docs/presets/watch.md) — this is process-local state, same footing as
    # the `only` filter a poller already publishes into its own state file.
    # #581: SOCK_PATH is now overridable, so a watcher spawned before an
    # operator changed SUPERTOOL_WATCH_SOCK keeps writing to the path it
    # started with. That is a real state, not a bug, but it must be
    # inspectable rather than inferred from "some events arrived and some
    # didn't" — a partial migration reads as a healthy board otherwise.
    current["sock_path"] = SOCK_PATH
    write_state(source, watcher_id, current)
    if notify_title and notify_message:
        desktop_notify(notify_title, notify_message)


def list_active_pids() -> list[dict[str, Any]]:
    """Scan /tmp for live watcher PID files. Stale entries are pruned in place.

    Returns rows with: source, id, pid, started (mtime ISO), state file existence
    flag, and last event from the state file when readable.
    """
    rows: list[dict[str, Any]] = []
    prefix = "supertool-watch-"
    suffix = ".pid"
    for name in sorted(os.listdir(STATE_DIR)):
        if not (name.startswith(prefix) and name.endswith(suffix)):
            continue
        path = os.path.join(STATE_DIR, name)
        try:
            pid = int(Path(path).read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            try:
                os.unlink(path)
            except OSError:
                pass
            continue
        # supertool-watch-{source}__{id}.pid
        stem = name[len(prefix):-len(suffix)]
        if "__" not in stem:
            continue
        source, watcher_id = stem.split("__", 1)
        if not _pid_alive(pid):
            # The row is still dropped — radar derives coverage from this
            # function and a dead PID is not coverage — but the death is
            # written down on the way past. Unlinking silently is what made a
            # lost watcher render exactly like one that never existed (#513).
            record_death(source, watcher_id, pid)
            try:
                os.unlink(path)
            except OSError:
                pass
            continue
        started = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(os.path.getmtime(path))
        )
        state = read_state(source, watcher_id)
        rows.append({
            "source": source,
            "id": watcher_id,
            "pid": pid,
            "started": started,
            "last_event": (state.get("last_event") or {}).get("event", ""),
            "last_event_ts": (state.get("last_event") or {}).get("ts", ""),
        })
    return rows


def poller_argv(source: str, watcher_id: str, only: list[str] | None = None) -> list[str]:
    """The exact command a labelled poller runs under.

    Two jobs, and they are the same job: it is what the grandchild execs into,
    and it is the signature every reader matches against. Keeping both sides on
    one function is deliberate — a label nobody can parse back and a matcher
    that looks for a label nobody writes fail identically and silently.

    The id is a whole argv element, never interpolated into one, so matching is
    token equality rather than a substring test: `33248` cannot match `332480`,
    and an id that happens to appear inside another process's arguments cannot
    be mistaken for a poller.

    argv[0] is a label and nothing else. The program that actually runs is
    handed to `os.execve` as its own first argument by
    `dispatcher._exec_labelled`, which returns early on `if not sys.executable`
    before getting there — so argv[0] is never executed. `_labelled` below
    scans for the dispatcher path followed by the `poll` sub-op and never reads
    tokens[0] — so argv[0] is never matched on either. It used to carry an
    `or "python3"` fallback, which rescued nothing (the branch it fired on
    cannot reach an exec at all) and cost a real misdiagnosis: #564 read this
    line as a Windows interpreter-resolution bet. #571.
    """
    argv = [
        sys.executable,
        str(Path(__file__).parent.resolve() / "dispatcher.py"),
        POLL_SUBOP,
        source,
        watcher_id,
    ]
    if only:
        argv.append("only=" + ",".join(only))
    return argv


def poller_env() -> dict[str, str]:
    """Environment for an exec'd poller: the caller's, plus where state lives."""
    env = dict(os.environ)
    env[STATE_DIR_ENV] = STATE_DIR
    return env


def _ps_rows() -> list[tuple[int, list[str]]] | None:
    """[(pid, argv tokens)] for every process, or None when `ps` cannot be read.

    None and [] are different answers and must stay that way: [] means nothing
    is running, None means nobody looked. #511 is a catalogue of what happens
    when a tool renders the second as the first.
    """
    try:
        proc = subprocess.run(
            ["ps", "-axww", "-o", "pid=,args="],
            capture_output=True, timeout=5, check=False,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    rows: list[tuple[int, list[str]]] = []
    for line in (proc.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        rows.append((pid, parts[1:]))
    return rows


def ps_scan_supported() -> bool:
    """Can a process scan ever answer on this machine?

    False means *permanently* no — there is no `ps` here at all, which is the
    normal case on Windows. Distinct from `scan_ok=False`, which means one
    particular scan did not answer: a `ps` that exists and timed out, exited
    non-zero, or could not be spawned is news; a `ps` that was never there is a
    property of the platform and cannot be news twice.

    Decided by looking for the binary, at the point where it would be run —
    not by platform name, and not by matching a failure message. Same shape as
    `docs/validators.md` §"Declining instead of guessing" makes at the raise
    site: everything that happens *after* a real `ps` is found stays a loud
    failure, because guessing towards silence there is how a genuinely broken
    scan starts looking clean.
    """
    return shutil.which("ps") is not None


def _labelled(tokens: list[str]) -> tuple[str, str] | None:
    """The (source, id) an argv announces, or None when it announces nothing.

    Requires the four tokens in sequence — `.../watch/dispatcher.py`, `poll`,
    SOURCE, ID — so the parent `dispatcher.py watch ...` invocation, `radar.py`,
    and a grep for any of them are all excluded.
    """
    for i, tok in enumerate(tokens):
        if not tok.replace("\\", "/").endswith(DISPATCHER_TAIL):
            continue
        if i + 3 < len(tokens) and tokens[i + 1] == POLL_SUBOP:
            return tokens[i + 2], tokens[i + 3]
    return None


def scan_poller_pids() -> tuple[dict[tuple[str, str], list[int]], bool]:
    """Every labelled poller on this machine, grouped by slot. Read-only.

    Returns ({(source, id): [pid, ...]}, scanned). One `ps` per call, not one
    per watcher: `watches` renders fifteen rows on this machine.

    Spawns nothing, signals nothing.
    """
    rows = _ps_rows()
    if rows is None:
        return {}, False
    found: dict[tuple[str, str], list[int]] = {}
    for pid, tokens in rows:
        key = _labelled(tokens)
        if key is None:
            continue
        found.setdefault(key, []).append(pid)
    for pids in found.values():
        pids.sort()
    return found, True


def watcher_pids(
    source: str,
    watcher_id: str,
    scan: tuple[dict[tuple[str, str], list[int]], bool] | None = None,
) -> dict[str, Any]:
    """Every live poller for one slot — the tracked one and any others.

    Keys: `tracked` (the pidfile's PID, 0 when there is none), `tracked_alive`,
    `pids` (all live pollers, sorted), `untracked` (those the pidfile does not
    name), `scan_ok`.

    `tracked` surviving as its own key is what lets a caller distinguish the
    three cases the old one-PID model collapsed into one: a slot with nothing
    in it, a slot whose recorded poller died without anyone noticing (#511 saw
    two of those, and the board was blind on both MRs), and a slot with pollers
    nobody recorded.
    """
    found, scan_ok = scan_poller_pids() if scan is None else scan
    tracked = read_pid(source, watcher_id)
    tracked_alive = bool(tracked and _pid_alive(tracked))
    live = [pid for pid in found.get((source, watcher_id), []) if _pid_alive(pid)]
    pids = sorted(set(live) | ({tracked} if tracked_alive else set()))
    return {
        "tracked": tracked,
        "tracked_alive": tracked_alive,
        "pids": pids,
        "untracked": [pid for pid in pids if pid != tracked],
        "scan_ok": scan_ok,
    }


def live_poller_pids(source: str, watcher_id: str) -> tuple[list[int], bool]:
    """(live labelled pollers for this slot, scanned). Ignores the pidfile."""
    found, scan_ok = scan_poller_pids()
    return [pid for pid in found.get((source, watcher_id), []) if _pid_alive(pid)], scan_ok


def list_watchers() -> tuple[list[dict[str, Any]], bool]:
    """`list_active_pids` widened by the process scan. ([row, ...], scanned).

    Adds to each row: `pids` (every live poller for that slot), `extra` (the
    ones the pidfile does not name) and `orphan` (True when there is no pidfile
    at all — the #511 case where deleting it left the process unreachable).

    Deliberately a second function rather than a change to `list_active_pids`:
    radar derives coverage from that one, and a row appearing there that no
    pidfile backs would change which MRs it believes are watched. This one only
    renders.
    """
    rows = list_active_pids()
    found, scan_ok = scan_poller_pids()
    seen: set[tuple[str, str]] = set()
    for row in rows:
        key = (str(row["source"]), str(row["id"]))
        seen.add(key)
        pids = sorted(set(found.get(key, [])) | {int(row["pid"])})
        row["pids"] = pids
        row["extra"] = [pid for pid in pids if pid != int(row["pid"])]
        row["orphan"] = False
        row["dead"] = False
        row["deaths"] = deaths(*key)
    for (source, watcher_id), pids in sorted(found.items()):
        if (source, watcher_id) in seen:
            continue
        live = sorted(pid for pid in pids if _pid_alive(pid))
        if not live:
            continue
        state = read_state(source, watcher_id)
        rows.append({
            "source": source,
            "id": watcher_id,
            "pid": live[0],
            "pids": live,
            "extra": live[1:],
            "orphan": True,
            "dead": False,
            "deaths": deaths(source, watcher_id),
            "started": "",
            "last_event": (state.get("last_event") or {}).get("event", ""),
            "last_event_ts": (state.get("last_event") or {}).get("ts", ""),
        })
    rows.extend(_lost_rows(seen | set(found)))
    return rows, scan_ok


def _lost_rows(covered: set[tuple[str, str]]) -> list[dict[str, Any]]:
    """A row for every slot that had a poller, lost it, and has no other.

    The whole of #513 in one function. `list_active_pids` prunes the stale pid
    file and omits the id, so the board rendered a lost watcher and a watcher
    that never existed identically — and "nothing to report" versus "not
    watching any more" are the two states a monitoring surface most needs to
    keep apart. The row persists until an operator acknowledges it with
    `unwatch`, or a poller covers the slot again; it is a supervision record,
    not a message that scrolls past once.
    """
    prefix = "supertool-watch-"
    suffix = ".state.json"
    out: list[dict[str, Any]] = []
    try:
        names = sorted(os.listdir(STATE_DIR))
    except OSError:
        return out
    for name in names:
        if not (name.startswith(prefix) and name.endswith(suffix)):
            continue
        stem = name[len(prefix):-len(suffix)]
        if "__" not in stem:
            continue
        source, watcher_id = stem.split("__", 1)
        if (source, watcher_id) in covered:
            continue
        recorded = deaths(source, watcher_id)
        if not recorded:
            continue
        state = read_state(source, watcher_id)
        out.append({
            "source": source,
            "id": watcher_id,
            "pid": 0,
            "pids": [],
            "extra": [],
            "orphan": False,
            "dead": True,
            "deaths": recorded,
            "started": "",
            "last_event": (state.get("last_event") or {}).get("event", ""),
            "last_event_ts": (state.get("last_event") or {}).get("ts", ""),
        })
    return out


# The liveness probe lives in presets/_proc.py so `gl-mrs` and `gh-prs` cannot
# drift from it again — three copies of these six lines is what produced both
# the WinError 87 escape (#422) and the TerminateProcess twin (#429).
_pid_alive = _proc.pid_alive
