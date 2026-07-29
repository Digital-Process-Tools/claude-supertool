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

SOCK_PATH = "/tmp/supertool-watch.sock"
STATE_DIR = "/tmp"

# Refuse to follow a pre-existing symlink at the pidfile path (#148's guard, in
# the second place that opens a /tmp path by predictable name). Windows has no
# such flag, and 0 leaves the open otherwise unchanged.
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


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

    Returns 0 when this process now owns the slot, else the PID of the poller
    that already does.

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
            try:
                os.unlink(pid_path(source, watcher_id))
            except FileNotFoundError:
                pass
            continue
        except OSError:
            return 0
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"{os.getpid()}\n")
        return 0
    return 0


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


def desktop_notify(title: str, message: str) -> None:
    """Fire-and-forget macOS notification. No-op elsewhere."""
    if sys.platform != "darwin":
        return
    if not shutil.which("osascript"):
        return
    body = message.replace('"', '\\"')
    head = title.replace('"', '\\"')
    script = f'display notification "{body}" with title "{head}"'
    try:
        subprocess.run(
            ["osascript", "-e", script],
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


# The liveness probe lives in presets/_proc.py so `gl-mrs` and `gh-prs` cannot
# drift from it again — three copies of these six lines is what produced both
# the WinError 87 escape (#422) and the TerminateProcess twin (#429).
_pid_alive = _proc.pid_alive
