#!/usr/bin/env python3
"""List running supertool MCP daemons.

Inspects /tmp/supertool-mcp-*.{sock,pid} pairs and reports name, pid, uptime, and last
activity. Discovers names by reading .supertool.json mcp block + hashing cwd+name to
match the socket; otherwise prints the hash-only entry for orphans.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import sys
import time
from pathlib import Path


def find_supertool_json() -> dict:
    d = os.path.abspath(os.getcwd())
    while True:
        p = os.path.join(d, ".supertool.json")
        if os.path.isfile(p):
            try:
                with open(p) as f:
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

    rows = []
    for pid_path in sorted(glob.glob("/tmp/supertool-mcp-*.pid")):
        h = Path(pid_path).stem.replace("supertool-mcp-", "")
        name = hash_to_name.get(h, "?")
        sock_path = f"/tmp/supertool-mcp-{h}.sock"
        log_path = f"/tmp/supertool-mcp-{h}.sock.log"
        try:
            pid = int(Path(pid_path).read_text().strip())
        except (OSError, ValueError):
            pid = 0
        alive = False
        if pid:
            try:
                os.kill(pid, 0)
                alive = True
            except (ProcessLookupError, PermissionError):
                pass
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
