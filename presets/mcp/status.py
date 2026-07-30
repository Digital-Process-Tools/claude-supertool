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

The same rule applies to the table itself (#551). A runtime dir that could not
be enumerated is not a runtime dir with nothing in it, so it is reported as
such rather than as `No supertool MCP daemons running.` — that line is a claim,
and it is only available to us once the listing succeeded.
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
from _paths import list_pidfiles, read_pid, runtime_dir  # noqa: E402


def find_supertool_json() -> tuple:
    """Return `(config, reason)`; a non-empty reason means we found one and could
    not read it.

    `reason` is empty exactly when the walk answered the question: either a
    config was read, or there is genuinely none between here and the
    filesystem root. This used to return a bare `{}` from both (#569), and
    those two mean opposite things to the reader. An empty config yields an
    empty `hash_to_name`, so **every** row prints `?` in the NAME column —
    which is the honest rendering for an orphan daemon and the misleading one
    for a config that would not parse. "Not declared in your config" sends you
    hunting a stray process; "your config is malformed" sends you to your
    JSON.

    Same shape as `list_pidfiles` (#551) and `_paths.read_pid` (#549): the
    reason travels with the value rather than being flattened into it. Unlike
    those two it is not a verdict about anything — `main()` keeps exiting `0`
    (#552) and prints this as a note above a table it still shows.

    A config that parses but is not an object gets the same treatment. It is
    not the absence this function reports, and it used to reach
    `cfg.get("mcp")` and raise `AttributeError` out of `mcp_status`.
    """
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
# The third state, as in docs/validators.md's "Declining instead of guessing":
# not a verdict about the daemon, a statement that we have none.
STATUS_UNKNOWN = "unknown"


# `read_pid` used to live here (#549) and now comes from `_paths`, imported
# above so `status.read_pid` still resolves. It moved because `stop.py` needed
# the same discrimination and had none (#569): the guard against non-positive
# pids existed only on the surface that reports, not on the one that signals.


def main() -> int:
    cfg, config_error = find_supertool_json()
    if config_error:
        # Above the table, on stdout, and not in place of it (#569). The rows
        # are still worth showing — what the reader loses is only the NAME
        # column, and this line is what stops `?` being read as "undeclared".
        # stdout for the same reason as the listing note below: `mcp_status`
        # exits 0 in every case, and the custom-op runner folds stderr into the
        # output only on a non-zero status.
        print(f"Cannot read config: {config_error}")
        print("  Daemon names cannot be resolved, so NAME shows `?` on every "
              "row — this is NOT a report that they are undeclared.")
    declared = (cfg.get("mcp") or {}).keys()
    hash_to_name = {hash_for(name): name for name in declared}

    base = runtime_dir()
    pidfiles, listing_error = list_pidfiles()
    if listing_error:
        # Not a row — no row was enumerated, so there is nothing to carry a
        # verdict. On stdout deliberately: every path `mcp_status` reaches
        # itself returns 0, and the custom-op runner only folds stderr into the
        # output on a non-zero status. A stderr-only line here would be #551
        # again, wearing a different coat. (`runtime_dir()` can refuse before
        # this function is entered — a stated exit, message on stderr, and the
        # non-zero status is what gets it folded in, #568. That is the one
        # non-zero exit and it never reaches this branch.)
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
        # `?`, not `0`: a row that could not name the pid must not print one.
        shown_pid = "?" if status == STATUS_UNKNOWN else str(pid)
        print(f"{name:<16} {h:<14} {shown_pid:<8} {status:<8} {up:<10} {idl:<10} {sock}")
        if reason:
            print(f"{'':<32}↳ {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
