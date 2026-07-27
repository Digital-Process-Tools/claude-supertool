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


# The liveness probe lives in presets/_proc.py so `gl-mrs` and `gh-prs` cannot
# drift from it again — three copies of these six lines is what produced both
# the WinError 87 escape (#422) and the TerminateProcess twin (#429).
_pid_alive = _proc.pid_alive
