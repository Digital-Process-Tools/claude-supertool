#!/usr/bin/env python3
"""List running supertool MCP daemons.

Inspects supertool-mcp-*.{sock,pid} pairs in the per-user runtime dir (#148) and
reports name, pid, uptime, and last
activity. Discovers names by reading .supertool.json mcp block + hashing cwd+name to
match the socket; otherwise prints the hash-only entry for orphans.
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
        try:
            pid = int(Path(pid_path).read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pid = 0
        alive = _proc.pid_alive(pid) if pid else False
        try: st = os.stat(pid_path); uptime = int(time.time() - st.st_mtime)
        except OSError: uptime = -1
        try: lst = os.stat(log_path); idle = int(time.time() - lst.st_mtime)
        except OSError: idle = -1
        rows.append((name, h, pid, "alive" if alive else "dead", uptime, idle, sock_path))

    if not rows:
        print("No supertool MCP daemons running.")
        return 0

    print(f"{'NAME':<16} {'HASH':<14} {'PID':<8} {'STATUS':<8} {'UPTIME':<10} {'IDLE':<10} SOCKET")
    for name, h, pid, status, uptime, idle, sock in rows:
        up = f"{uptime}s" if uptime >= 0 else "-"
        idl = f"{idle}s" if idle >= 0 else "-"
        print(f"{name:<16} {h:<14} {pid:<8} {status:<8} {up:<10} {idl:<10} {sock}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
