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

SOCK_PATH = "/tmp/supertool-watch.sock"
STATE_DIR = "/tmp"


def state_path(source: str, watcher_id: str) -> str:
    return f"{STATE_DIR}/supertool-watch-{source}__{watcher_id}.state.json"


def pid_path(source: str, watcher_id: str) -> str:
    return f"{STATE_DIR}/supertool-watch-{source}__{watcher_id}.pid"


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
) -> None:
    """All transports in one call.

    Writes the event to the UDS socket (if any listener), refreshes the
    status file with the latest event, and optionally fires a desktop
    notification when title+message are provided.
    """
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": source,
        "id": watcher_id,
        "event": event_key,
        "payload": payload,
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
            pid = int(Path(path).read_text().strip())
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


# GetExitCodeProcess reports this for a process that has not exited.
_WIN_STILL_ACTIVE = 259
# Read-only access right. Deliberately not PROCESS_ALL_ACCESS: the probe must
# never hold a handle powerful enough to terminate what it is inspecting.
_WIN_QUERY_LIMITED_INFORMATION = 0x1000


def _kernel32():
    """Seam so the Windows probe can be exercised from a POSIX test runner."""
    import ctypes
    return ctypes.windll.kernel32  # type: ignore[attr-defined]


def _pid_alive_windows(pid: int) -> bool:
    """Non-destructive liveness probe: open for query only, read exit code."""
    import ctypes
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_WIN_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == _WIN_STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _pid_alive(pid: int) -> bool:
    """Is this PID a live process? Always answers — never raises, never kills.

    On Windows `os.kill(pid, 0)` is not a liveness probe. Python documents any
    signal other than CTRL_C_EVENT/CTRL_BREAK_EVENT as routed to
    TerminateProcess, so the POSIX idiom would kill the very watcher it was
    asked about; and for a PID that does not exist OpenProcess fails with
    WinError 87, an OSError that is neither ProcessLookupError nor
    PermissionError, so it escaped this function and took `radar` down with
    it. Windows therefore gets an explicit read-only probe.

    An unanswerable question resolves to "not alive", because that is the
    safe direction: the caller reacts by respawning or pruning, and a
    duplicate poller is visible and cheap while a poller everyone believes is
    running is exactly the silent blindness this subsystem exists to remove.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            return _pid_alive_windows(pid)
        except (OSError, AttributeError, ValueError):
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
