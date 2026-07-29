#!/usr/bin/env python3
"""Stop one or all supertool MCP daemons.

Usage:
    python3 stop.py NAME    # SIGTERM the daemon for NAME (using .supertool.json + cwd)
    python3 stop.py --all   # stop every supertool-mcp-*.pid found on disk

SIGTERM gives the daemon a chance to clean up its socket + cclsp subprocess. Falls back
to SIGKILL if it doesn't exit within 3s.

The exit status is the only thing the automatic caller can read (#547):
`supertool.py`'s new-file invalidation path runs this as a subprocess and has
no other channel. So the status has to carry the outcome, not merely "it ran".
It previously returned 0 even when the process was still alive after SIGKILL —
which is #239 exactly, and the one case the caller most needs to hear about.

Failure reasons go to stderr, successes to stdout, so the two are separable
by stream as well as by code.
"""
from __future__ import annotations

import hashlib
import os
import signal
import sys
import time
from pathlib import Path

# Shared path helpers (#148): per-user runtime dir, NOT /tmp.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _proc  # noqa: E402  (the one liveness probe — never os.kill(pid, 0), #429)
from _paths import list_pidfiles, socket_pid_paths  # noqa: E402


EXIT_OK = 0            # the daemon was running and is now gone
EXIT_NO_DAEMON = 1     # nothing was running — benign, nothing to invalidate
EXIT_USAGE = 2         # bad arguments
EXIT_STOP_FAILED = 3   # a daemon was found and it is still there (or unknowable)
EXIT_REFUSED = 4       # no claim established — an unverifiable or unlistable runtime dir


def stop_pid(pid: int) -> bool:
    """SIGTERM, wait, SIGKILL on hang. Returns True if process is gone after."""
    try: os.kill(pid, signal.SIGTERM)
    except ProcessLookupError: return True
    except PermissionError: return False
    deadline = time.time() + 3
    while time.time() < deadline:
        if not _proc.pid_alive(pid): return True
        time.sleep(0.1)
    try: os.kill(pid, signal.SIGKILL)
    except ProcessLookupError: return True
    time.sleep(0.5)
    return not _proc.pid_alive(pid)


def stop_by_pidfile(pid_path: str) -> tuple:
    """Stop the daemon named by `pid_path`. Returns (ok, message).

    An unreadable pidfile is `ok=False`: we did not stop anything and we
    cannot say whether anything is still running, which is the same
    unanswered question as a failed kill, not a success.
    """
    try:
        pid = int(Path(pid_path).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False, f"  {pid_path}: invalid pidfile"
    if stop_pid(pid):
        # Daemon cleanup unlinks pidfile, but force-unlink in case SIGKILL was needed
        try: os.unlink(pid_path)
        except FileNotFoundError: pass
        return True, f"  stopped pid={pid} ({pid_path})"
    return False, f"  failed to stop pid={pid} ({pid_path})"


def _refused(exc: SystemExit) -> int:
    """Relabel a stated refusal from the runtime-dir checks as EXIT_REFUSED.

    `runtime_dir()` refuses with `sys.exit("<reason>")`, which exits 1 — the
    same code as "no daemon found". Left alone, the single check whose job is
    to be suspicious would be read by the caller as the most benign outcome
    there is. A bare numeric exit carries no reason and is not a refusal, so
    it propagates untouched rather than being relabelled on a guess.
    """
    reason = exc.code
    if reason is None or isinstance(reason, int):
        raise exc
    sys.stderr.write(f"{reason}\n")
    return EXIT_REFUSED


def main(argv: list) -> int:
    if len(argv) < 2:
        sys.stderr.write("usage: stop.py NAME | --all\n")
        return EXIT_USAGE
    if argv[1] == "--all":
        try:
            pidfiles, reason = list_pidfiles()
        except SystemExit as exc:
            return _refused(exc)
        if reason:
            # NOT EXIT_OK (#551). `--all` deliberately reports 0 when nothing is
            # running, because that is benign — nothing stale can come from
            # nothing. A runtime dir we failed to enumerate is not that state:
            # we stopped nothing and we cannot say whether anything is left. It
            # joins runtime_dir()'s stated refusals under EXIT_REFUSED, which
            # already maps to ok=False for the caller, rather than earning a new
            # code for a distinction nothing downstream would act on.
            sys.stderr.write(
                f"{reason}\n"
                f"Refusing to report on daemons we could not enumerate: "
                f"nothing was stopped, and this is not a statement that "
                f"nothing was running.\n"
            )
            return EXIT_REFUSED
        if not pidfiles:
            print("No daemons running.")
            return EXIT_OK
        print(f"Stopping {len(pidfiles)} daemon(s):")
        failed = 0
        for p in pidfiles:
            ok, message = stop_by_pidfile(p)
            if ok:
                print(message)
            else:
                failed += 1
                sys.stderr.write(f"{message}\n")
        return EXIT_STOP_FAILED if failed else EXIT_OK

    name = argv[1]
    cwd = os.path.abspath(os.getcwd())
    try:
        _sock_path, pid_path = socket_pid_paths(cwd, name)
    except SystemExit as exc:
        return _refused(exc)
    if not os.path.exists(pid_path):
        print(f"No daemon found for '{name}' (expected {pid_path})")
        return EXIT_NO_DAEMON
    print(f"Stopping daemon '{name}':")
    ok, message = stop_by_pidfile(pid_path)
    if ok:
        print(message)
        return EXIT_OK
    sys.stderr.write(f"{message}\n")
    return EXIT_STOP_FAILED


if __name__ == "__main__":
    sys.exit(main(sys.argv))
