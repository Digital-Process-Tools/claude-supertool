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

# Shared path helpers (#148): per-user runtime dir, NOT /tmp.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _proc  # noqa: E402  (the one liveness probe — never os.kill(pid, 0), #429)
from _paths import list_pidfiles, read_pid, socket_pid_paths  # noqa: E402


# `1` is deliberately not used and must stay that way (#574). It is the status
# CPython gives an uncaught exception, so it is the one number this file cannot
# also spend on a meaning of its own: a `stop.py` that died before it looked at
# anything would be spelled identically to whatever `1` was assigned. It used to
# be `EXIT_NO_DAEMON`, i.e. the most reassuring answer there is, and
# `supertool.py` read it as `ok` — a successful invalidation reported by a script
# that never ran. The gap starting at `5` is that reservation, not an oversight.
EXIT_OK = 0            # the daemon was running and is now gone
EXIT_USAGE = 2         # bad arguments
EXIT_STOP_FAILED = 3   # a daemon was found and it is still there (or unknowable)
EXIT_REFUSED = 4       # no claim established — an unverifiable or unlistable runtime dir
EXIT_NO_DAEMON = 5     # nothing was running — benign, nothing to invalidate


def stop_pid(pid: int) -> bool:
    """SIGTERM, wait, SIGKILL on hang. Returns True if process is gone after.

    Refuses any non-positive `pid` without signalling anything (#569).
    `os.kill` is a selector, not an identity: `0` is the caller's own process
    group, `-1` is every process the caller may signal, and `-N` is process
    group `N` — see `_paths.read_pid` for the full rule. A pidfile records one
    daemon, so only a positive value can be one, and passing anything else
    here broadcasts rather than mistargets.

    `False` rather than an exception: the callers of this function are already
    built to report a stop that did not happen, and a traceback out of
    `main()` exits `1`, which is not a code this file gets to choose the
    meaning of. It read as `no-daemon`, i.e. success, until #574 vacated it;
    it now reads as `crashed`. Either way a refusal belongs on a code stated
    deliberately, not on whatever the interpreter exits with on the way out.

    The guard is here as well as in `stop_by_pidfile` because this function is
    module-level and reachable from any caller; a check that lived only at
    today's single call site would protect only today's single call site. It
    is also not covered by the wait loop below: `_proc.pid_alive` rejects
    `pid <= 0` (#429), so for these values the loop would return on its first
    iteration — the SIGTERM goes out, the SIGKILL never does, and the function
    reports the process gone.
    """
    if pid <= 0: return False
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

    The read goes through `_paths.read_pid` (#569). This used to be a bare
    `int(...)` under `except (OSError, ValueError)`, which let `0`, `+0` and
    `-1` through — `int()` parses all three — into `os.kill`, where they are
    process-group selectors rather than pids. It also collapsed four different
    causes into one message, `invalid pidfile`: permission denied, a mid-write
    empty file, `EIO`, and genuine garbage have different fixes, and this is
    the surface `docs/mcp-integration.md` sends the reader *from*.

    The corrupt pidfile is deliberately **not** unlinked. It is the only
    evidence of whatever wrote it, and deleting it would manufacture exactly
    the reading refused here: the next `mcp_stop` would find no file, report
    `EXIT_NO_DAEMON` and be counted as `ok` for a daemon whose fate is still
    unknown. `mcp_status` also needs the file to render the `unknown` row the
    docs send the reader to. It stays until a human looks at it, and it keeps
    failing loudly until then, which is the correct amount of noise for a
    state nobody has explained.
    """
    pid, reason = read_pid(pid_path)
    if reason:
        return False, f"  {pid_path}: {reason}"
    if stop_pid(pid):
        # Daemon cleanup unlinks pidfile, but force-unlink in case SIGKILL was needed
        try: os.unlink(pid_path)
        except FileNotFoundError: pass
        return True, f"  stopped pid={pid} ({pid_path})"
    return False, f"  failed to stop pid={pid} ({pid_path})"


def _refused(exc: SystemExit) -> int:
    """Relabel a stated refusal from the runtime-dir checks as EXIT_REFUSED.

    `runtime_dir()` refuses with `sys.exit("<reason>")`, which exits 1. That
    used to be "no daemon found" as well, so the single check whose job is to
    be suspicious was read by the caller as the most benign outcome there is;
    since #574 vacated `1` it is read as a crash, which at least has the sign
    right, but it is still the wrong sentence for a refusal that was stated
    on purpose and printed its reason. A bare numeric exit carries no reason
    and is not a refusal, so it propagates untouched rather than being
    relabelled on a guess.
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
