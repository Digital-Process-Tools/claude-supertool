#!/usr/bin/env python3
"""List running supertool MCP daemons.

Inspects supertool-mcp-*.{sock,pid} pairs in the per-user runtime dir (#148) and
reports name, pid, uptime, and last
activity. Discovers names by reading .supertool.json mcp block + hashing cwd+name to
match the socket; otherwise prints the hash-only entry for orphans.

STATUS has three values, not two (#549). A pidfile we could not read tells us
nothing about the process behind it, so it reports `unknown` with the reason,
never `dead` — `dead` is reserved for a pid we actually read and actually
probed. This is the one op a human runs to find out whether a daemon is alive;
of all the surfaces that could afford to guess, this is not one of them.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

# Shared path helpers (#148): per-user runtime dir, NOT /tmp.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _proc  # noqa: E402  (the one liveness probe — never os.kill(pid, 0), #429)
from _paths import list_pidfiles, runtime_dir  # noqa: E402


def find_supertool_json() -> dict:
    d = os.path.abspath(os.getcwd())
    while True:
        p = os.path.join(d, ".supertool.json")
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, json.JSONDecodeError):
                return {}
        parent = os.path.dirname(d)
        if parent == d:
            return {}
        d = parent


def hash_for(name: str) -> str:
    cwd = os.path.abspath(os.getcwd())
    return hashlib.sha1(f"{cwd}::{name}".encode()).hexdigest()[:12]


STATUS_ALIVE = "alive"
STATUS_DEAD = "dead"
# The third state, as in docs/validators.md's "Declining instead of guessing":
# not a verdict about the daemon, a statement that we have none.
STATUS_UNKNOWN = "unknown"


def read_pid(pid_path: str) -> tuple:
    """Return `(pid, reason)`; a non-empty reason means the pid is unknowable.

    `reason` is empty exactly when `pid` is a number we actually read off disk.
    Every other outcome — unreadable file, unparsable contents, a value that is
    not a process id — returns `0` *and* a reason, and the caller must render
    `STATUS_UNKNOWN` rather than probe a pid it does not have.

    This used to return `0` alone (#549). Zero is falsy, the probe was skipped,
    and the row printed `dead` — not a cautious reading of an unreadable file
    but one of the two possible answers, asserted. `stop.py` already calls the
    same pidfile a failure rather than a success (#547); a `status` that
    disagreed with it would mislead the reader sent there to confirm.

    An empty file gets its own reason because it has a specific cause worth
    naming: a daemon caught between `open` and `write`.
    """
    try:
        raw = Path(pid_path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        return 0, f"unreadable pidfile: {exc.strerror or exc}"
    if not raw:
        return 0, "empty pidfile — a daemon may be mid-write"
    try:
        pid = int(raw)
    except ValueError:
        return 0, f"unparsable pidfile: {raw[:40]!r}"
    if pid <= 0:
        return 0, f"pidfile holds {pid}, which is not a process id"
    return pid, ""


def main() -> int:
    cfg = find_supertool_json()
    declared = (cfg.get("mcp") or {}).keys()
    hash_to_name = {hash_for(name): name for name in declared}

    base = runtime_dir()
    rows = []
    for pid_path in list_pidfiles():
        h = Path(pid_path).stem.replace("supertool-mcp-", "")
        name = hash_to_name.get(h, "?")
        sock_path = os.path.join(base, f"supertool-mcp-{h}.sock")
        log_path = os.path.join(base, f"supertool-mcp-{h}.sock.log")
        pid, reason = read_pid(pid_path)
        if reason:
            status = STATUS_UNKNOWN
        else:
            status = STATUS_ALIVE if _proc.pid_alive(pid) else STATUS_DEAD
        try: st = os.stat(pid_path); uptime = int(time.time() - st.st_mtime)
        except OSError: uptime = -1
        try: lst = os.stat(log_path); idle = int(time.time() - lst.st_mtime)
        except OSError: idle = -1
        rows.append((name, h, pid, status, reason, uptime, idle, sock_path))

    if not rows:
        print("No supertool MCP daemons running.")
        return 0

    print(f"{'NAME':<16} {'HASH':<14} {'PID':<8} {'STATUS':<8} {'UPTIME':<10} {'IDLE':<10} SOCKET")
    for name, h, pid, status, reason, uptime, idle, sock in rows:
        up = f"{uptime}s" if uptime >= 0 else "-"
        idl = f"{idle}s" if idle >= 0 else "-"
        # `?`, not `0`: a row that could not name the pid must not print one.
        shown_pid = "?" if status == STATUS_UNKNOWN else str(pid)
        print(f"{name:<16} {h:<14} {shown_pid:<8} {status:<8} {up:<10} {idl:<10} {sock}")
        if reason:
            print(f"{'':<32}↳ {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
