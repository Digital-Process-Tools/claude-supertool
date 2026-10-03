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
from typing import Optional

# Shared path helpers (#148): per-user runtime dir, NOT /tmp.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _proc  # noqa: E402  (the one liveness probe — never os.kill(pid, 0), #429)
from _paths import (  # noqa: E402,F401
    list_pidfiles,
    open_runtime_dir,
    read_pid,
    socket_pid_names,
    socket_pid_paths,  # re-exported: the path form is still how rows are named
)


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


def stop_by_pidfile(pid_path: str, *, dir_fd: Optional[int] = None) -> tuple:
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

    With `dir_fd`, the read and the unlink are resolved against the validated
    runtime-dir descriptor and `pid_path` is a basename (#598); `where` is kept
    for the messages, which have to name something an operator can type. Without
    it both are by path, for callers that only ever had one. The unlink is the
    reason this matters more than it looks: it is the one place `stop.py`
    *writes*, and a directory swapped in after `list_pidfiles` validated one
    would have it deleting a file of someone else's choosing.
    """
    where = pid_path if dir_fd is None else os.path.join(_runtime_hint(), pid_path)
    kw = {} if dir_fd is None else {"dir_fd": dir_fd}
    pid, reason = read_pid(pid_path, dir_fd=dir_fd)
    if reason:
        return False, f"  {where}: {reason}"
    if stop_pid(pid):
        # Daemon cleanup unlinks pidfile, but force-unlink in case SIGKILL was needed
        try: os.unlink(pid_path, **kw)
        except FileNotFoundError: pass
        return True, f"  stopped pid={pid} ({where})"
    return False, f"  failed to stop pid={pid} ({where})"


_RUNTIME_HINT = [""]


def _runtime_hint() -> str:
    """The resolved runtime dir, for messages only, remembered from the open."""
    return _RUNTIME_HINT[0]


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
    # This invocation's hint, not the process's (#686). Same reasoning as
    # `_UNANSWERED` in presets/git/status.py: in production a preset is a
    # subprocess and the two are the same thing, but under a harness that
    # imports this module once and calls main() repeatedly, a base left behind
    # by an earlier run would caption this one's messages with a directory it
    # never opened. Empty is the import-time value, and _runtime_hint() joining
    # "" is exactly the pre-resolution behaviour.
    _RUNTIME_HINT[0] = ""
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
        return _stop_each([os.path.basename(p) for p in pidfiles])

    name = argv[1]
    cwd = os.path.abspath(os.getcwd())
    try:
        _sock_name, pid_name = socket_pid_names(cwd, name)
        fd, base = open_runtime_dir()
    except SystemExit as exc:
        return _refused(exc)
    _RUNTIME_HINT[0] = base
    try:
        if not os.path.exists(os.path.join(base, pid_name)):
            print(f"No daemon found for '{name}' "
                  f"(expected {os.path.join(base, pid_name)})")
            return EXIT_NO_DAEMON
        print(f"Stopping daemon '{name}':")
        ok, message = stop_by_pidfile(pid_name, dir_fd=fd)
    finally:
        os.close(fd)
    if ok:
        print(message)
        return EXIT_OK
    sys.stderr.write(f"{message}\n")
    return EXIT_STOP_FAILED


def _stop_each(pid_names: list) -> int:
    """Signal every named daemon, all under one held runtime-dir descriptor (#598).

    `list_pidfiles` already validated and enumerated the directory through a
    descriptor, but it closed it and returned joined strings, so `--all` used to
    re-resolve each of them at unlink time. One descriptor for the whole sweep
    means the files removed are provably the files listed.
    """
    try:
        fd, base = open_runtime_dir()
    except SystemExit as exc:
        return _refused(exc)
    _RUNTIME_HINT[0] = base
    failed = 0
    try:
        for pid_name in pid_names:
            ok, message = stop_by_pidfile(pid_name, dir_fd=fd)
            if ok:
                print(message)
            else:
                failed += 1
                sys.stderr.write(f"{message}\n")
    finally:
        os.close(fd)
    return EXIT_STOP_FAILED if failed else EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv))
