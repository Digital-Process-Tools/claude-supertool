#!/usr/bin/env python3


















from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _console import use_utf8_stdout  
import _proc  
from _paths import list_pidfiles, read_pid, runtime_dir  


def find_supertool_json() -> tuple:






















    d = os.path.abspath(os.getcwd())
    while True:
        p = os.path.join(d, ".supertool.json")
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    cfg = json.load(f)
            except OSError as exc:
                return {}, f"{p}: could not be read: {exc.strerror or exc}"
            except json.JSONDecodeError as exc:
                return {}, f"{p}: could not be parsed: {exc}"
            if not isinstance(cfg, dict):
                return {}, (f"{p}: could not be used: top level is "
                            f"{type(cfg).__name__}, not a JSON object")
            return cfg, ""
        parent = os.path.dirname(d)
        if parent == d:
            return {}, ""
        d = parent


def hash_for(name: str) -> str:
    cwd = os.path.abspath(os.getcwd())
    return hashlib.sha1(f"{cwd}::{name}".encode()).hexdigest()[:12]


STATUS_ALIVE = "alive"
STATUS_DEAD = "dead"


STATUS_UNKNOWN = "unknown"








def main() -> int:
    use_utf8_stdout()
    cfg, config_error = find_supertool_json()
    if config_error:






        print(f"Cannot read config: {config_error}")
        print("  Daemon names cannot be resolved, so NAME shows `?` on every "
              "row — this is NOT a report that they are undeclared.")
    declared = (cfg.get("mcp") or {}).keys()
    hash_to_name = {hash_for(name): name for name in declared}

    base = runtime_dir()
    pidfiles, listing_error = list_pidfiles()
    if listing_error:








        print(f"Cannot list supertool MCP daemons: {listing_error}")
        print("  The runtime dir could not be read, so this is NOT a report "
              "that none are running.")
        return 0

    rows = []
    for pid_path in pidfiles:
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

        shown_pid = "?" if status == STATUS_UNKNOWN else str(pid)
        print(f"{name:<16} {h:<14} {shown_pid:<8} {status:<8} {up:<10} {idl:<10} {sock}")
        if reason:
            print(f"{'':<32}↳ {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
