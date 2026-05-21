#!/usr/bin/env python3
"""Stop one or all supertool MCP daemons.

Usage:
    python3 stop.py NAME    # SIGTERM the daemon for NAME (using .supertool.json + cwd)
    python3 stop.py --all   # stop every supertool-mcp-*.pid found on disk

SIGTERM gives the daemon a chance to clean up its socket + cclsp subprocess. Falls back
to SIGKILL if it doesn't exit within 3s.
"""
from __future__ import annotations

import glob
import hashlib
import os
import signal
import sys
import time
from pathlib import Path


def stop_pid(pid: int) -> bool:
    """SIGTERM, wait, SIGKILL on hang. Returns True if process is gone after."""
    try: os.kill(pid, signal.SIGTERM)
    except ProcessLookupError: return True
    except PermissionError: return False
    deadline = time.time() + 3
    while time.time() < deadline:
        try: os.kill(pid, 0)
        except ProcessLookupError: return True
        time.sleep(0.1)
    try: os.kill(pid, signal.SIGKILL)
    except ProcessLookupError: return True
    time.sleep(0.5)
    try: os.kill(pid, 0); return False
    except ProcessLookupError: return True


def stop_by_pidfile(pid_path: str) -> str:
    try:
        pid = int(Path(pid_path).read_text().strip())
    except (OSError, ValueError):
        return f"  {pid_path}: invalid pidfile"
    if stop_pid(pid):
        # Daemon cleanup unlinks pidfile, but force-unlink in case SIGKILL was needed
        try: os.unlink(pid_path)
        except FileNotFoundError: pass
        return f"  stopped pid={pid} ({pid_path})"
    return f"  failed to stop pid={pid} ({pid_path})"


def main(argv: list) -> int:
    if len(argv) < 2:
        sys.stderr.write("usage: stop.py NAME | --all\n")
        return 2
    if argv[1] == "--all":
        pidfiles = sorted(glob.glob("/tmp/supertool-mcp-*.pid"))
        if not pidfiles:
            print("No daemons running.")
            return 0
        print(f"Stopping {len(pidfiles)} daemon(s):")
        for p in pidfiles:
            print(stop_by_pidfile(p))
        return 0

    name = argv[1]
    cwd = os.path.abspath(os.getcwd())
    h = hashlib.sha1(f"{cwd}::{name}".encode()).hexdigest()[:12]
    pid_path = f"/tmp/supertool-mcp-{h}.pid"
    if not os.path.exists(pid_path):
        print(f"No daemon found for '{name}' (expected {pid_path})")
        return 1
    print(f"Stopping daemon '{name}':")
    print(stop_by_pidfile(pid_path))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
