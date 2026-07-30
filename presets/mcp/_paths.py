"""Shared path / runtime-dir helpers for the MCP daemon family.

Closes #148. The previous design placed the daemon's UDS socket, pidfile,
and log under `/tmp/supertool-mcp-<hash>.{sock,pid,log,stderr}`. `/tmp` is
mode `1777` (world-writable) and the hash is deterministic from `cwd + name`,
both of which an attacker on the same machine can predict for any project
they know the layout of. That enabled:

- **Pidfile DoS** — co-tenant writes `1` (init) into the pidfile; daemon's
  `os.kill(1, 0)` succeeds → daemon refuses to start.
- **Symlink overwrite** — co-tenant creates `<sock>.stderr` as a symlink to
  a victim file; daemon opens it with `"ab"` → follows symlink → overwrites.
- **UDS pre-bind** — between `bind()` and `chmod(0o700)` another local user
  can `connect()`. On macOS, `chmod` on a UDS is racey; on Linux, parent-
  dir perms (1777) gate connect, so the chmod barely matters.

Fix: move everything under a per-user runtime dir (`$XDG_RUNTIME_DIR/supertool/`
on Linux, `~/Library/Caches/supertool/mcp/` on macOS, `~/.cache/supertool/mcp/`
fallback) created mode `0700` and ownership-checked. Opens use
`O_NOFOLLOW | O_CREAT | O_EXCL` so symlink targets are rejected, not
followed.
"""
from __future__ import annotations

import hashlib
import os
import stat
import sys
from pathlib import Path
from typing import Tuple


def runtime_dir() -> str:
    """Return the per-user runtime directory for supertool MCP daemons.

    Order of preference:
      1. `$SUPERTOOL_RUNTIME_DIR` (explicit override — tests, custom setups)
      2. `$XDG_RUNTIME_DIR/supertool/mcp/` (Linux / freedesktop spec)
      3. `~/Library/Caches/supertool/mcp/` (macOS)
      4. `~/.cache/supertool/mcp/` (fallback)

    Creates the directory with mode `0700` if missing. If it exists but is
    owned by another uid (a co-tenant squatting), aborts with an error —
    we will not trust a directory we don't own. Also aborts when the
    directory is not owner-only and cannot be made so (#568): the mode is
    verified after the chmod rather than assumed from it.
    """
    override = os.environ.get("SUPERTOOL_RUNTIME_DIR")
    if override:
        base = Path(override)
    else:
        xdg = os.environ.get("XDG_RUNTIME_DIR")
        if xdg and Path(xdg).is_dir():
            base = Path(xdg) / "supertool" / "mcp"
        elif sys.platform == "darwin":
            base = Path.home() / "Library" / "Caches" / "supertool" / "mcp"
        else:
            base = Path.home() / ".cache" / "supertool" / "mcp"

    # Created owner-only, not created loose and tightened afterwards (#568).
    # `mkdir` with no `mode=` uses `0o777 & ~umask` — `0o755` under the common
    # `umask 022` — so every first run had a window between the mkdir and the
    # chmod in which this directory was group- and world-traversable. That is
    # the window #148 exists to close, since on Linux it is the parent dir's
    # mode, not the socket's, that gates a co-tenant's connect().
    #
    # An OSError here is a stated refusal rather than a traceback out of a
    # library helper. `exist_ok=True` does not tolerate a non-directory, so a
    # `SUPERTOOL_RUNTIME_DIR` naming a regular file raised `FileExistsError`
    # from inside pathlib; a read-only parent raised `PermissionError`. Both
    # reached callers as the #544 shape — a crash where a sentence belongs.
    try:
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        sys.exit(
            f"daemon: cannot create runtime dir {base}: "
            f"{exc.strerror or exc}. Set SUPERTOOL_RUNTIME_DIR to a path you "
            f"can create as a directory."
        )
    # The chmod still has a job. `mode=` above applies only to the leaf and
    # only when we are the ones creating it: it is ignored for a directory
    # that already exists, and ignored for the intermediate directories
    # `parents=True` makes. Its result is checked below rather than assumed —
    # the `except OSError: pass` that used to be the whole of the handling
    # left the requirement stated in a comment and enforced by nothing.
    try:
        os.chmod(base, 0o700)
    except OSError:
        pass
    # Ownership check — refuse to trust a directory another uid created.
    #
    # Where `os.geteuid` does not exist the comparison is not merely
    # unavailable, it is unanswerable: `st_uid` is a constant 0 on Windows and
    # carries no ownership information at all (#544). So this refuses rather
    # than waving the check through. Defaulting it to "ours" would trade a loud
    # failure for a quiet one on the single check whose whole job is to be
    # suspicious, and a security check that silently stops running is
    # indistinguishable from one that keeps passing.
    #
    # No warm validator reaches here on such a platform any more — the adapters
    # decline for want of AF_UNIX first — but stop.py, status.py and
    # supertool.py's MCP client all call this too, and the next caller should
    # meet a sentence rather than an AttributeError.
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        sys.exit(
            f"daemon: cannot verify ownership of runtime dir {base} on this "
            f"platform — os.geteuid does not exist and st_uid is a constant "
            f"here, so the question cannot be answered rather than merely "
            f"being unavailable. Refusing to use it. Set SUPERTOOL_RUNTIME_DIR "
            f"to a directory you own on a platform where ownership is checkable."
        )
    try:
        st = os.stat(base)
        if st.st_uid != geteuid():
            sys.exit(
                f"daemon: runtime dir {base} owned by uid {st.st_uid}, "
                f"not us ({geteuid()}). Refusing to use it. "
                f"Set SUPERTOOL_RUNTIME_DIR to a directory you own."
            )
    except OSError as e:
        sys.exit(f"daemon: cannot stat {base}: {e}")
    # Mode check — the requirement the comment above the chmod always claimed
    # (#568). `os.stat` answers this, so a loose mode is a finding and not the
    # absence of one: `skipped` is for a question that cannot be asked, which
    # is what `st_uid` is on the platforms #544 covers, and is not what a
    # readable `0o755` is. Refusing rather than warning follows the same
    # argument as the ownership check it shares this stat with, and matches
    # `_publish_safety.check_token_file_mode`, which declines an insecure
    # token file the way `ssh` declines an insecure key. A warning here would
    # be a security check that never stops anything, which is #544's lesson
    # read backwards.
    #
    # `& 0o077` rather than `!= 0o700`: the question is exposure to other
    # users, so a group-read-only dir fails it and an owner-only `0o600` — odd
    # but not exposed — passes it and is left to fail elsewhere on its own
    # terms if it is going to.
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o077:
        sys.exit(
            f"daemon: runtime dir {base} is {oct(mode)}, not owner-only, and "
            f"the chmod to 0700 did not take. On Linux it is this directory's "
            f"mode that gates a co-tenant's connect() to the daemon socket and "
            f"their enumeration of the pidfiles (#148), so this is exposure "
            f"rather than untidiness. Refusing to use it. Fix it with "
            f"`chmod 700 {base}` — or, if this is a filesystem with no POSIX "
            f"modes (exFAT/FAT32/SMB), remount it with `umask=077` or point "
            f"SUPERTOOL_RUNTIME_DIR at a filesystem that has them."
        )
    return str(base)


def socket_pid_paths(cwd: str, name: str) -> Tuple[str, str]:
    """Return (sock_path, pid_path) for `name` inside cwd's daemon group.

    Hash is sha1(cwd::name)[:12] — same as the old /tmp layout for path
    stability across upgrades, just under the trusted runtime dir.
    """
    h = hashlib.sha1(f"{cwd}::{name}".encode()).hexdigest()[:12]
    base = os.path.join(runtime_dir(), f"supertool-mcp-{h}")
    return f"{base}.sock", f"{base}.pid"


def list_pidfiles() -> Tuple[list, str]:
    """Return `(pidfiles, reason)`; a non-empty reason means we could not look.

    `reason` is empty exactly when `os.listdir` succeeded, and only then does an
    empty list mean *there are no daemons*. Any other outcome returns `[]` *and*
    a reason, and the caller must state that instead of reporting an absence it
    never established.

    This used to swallow the `OSError` and return `[]` alone (#551). Both
    callers read that as proof of absence: `status.py` printed `No supertool MCP
    daemons running.` and `stop.py --all` printed `No daemons running.` and
    exited `EXIT_OK` — the code #547 documents as "nothing stale can come from
    nothing", which is only true once we have looked. Same shape as `read_pid`
    in `status.py` (#549), one layer up: a directory we could not enumerate is
    the whole table unavailable, not a row carrying a verdict.

    Reachability is low by construction, and worth stating rather than
    inflating. `runtime_dir()` runs first, chmods the directory back to `0700`
    (so a `chmod 000` heals instead of raising) and exits outright on foreign
    ownership or on a mode it could not tighten (#568), which leaves an `ENOENT`
    race against its own `mkdir`, `EIO` on a failing volume, and `EMFILE` under
    fd pressure. Fixed anyway because the
    failure is silent by construction — before this, nothing on either surface
    could ever report that it had happened.
    """
    base = runtime_dir()
    try:
        names = os.listdir(base)
    except OSError as exc:
        return [], f"cannot list runtime dir {base}: {exc.strerror or exc}"
    return sorted(
        os.path.join(base, name)
        for name in names
        if name.startswith("supertool-mcp-") and name.endswith(".pid")
    ), ""

def read_pid(pid_path: str) -> Tuple[int, str]:
    """Return `(pid, reason)`; a non-empty reason means the pid is unknowable.

    `reason` is empty exactly when `pid` is a number we actually read off disk
    *and* that number can only mean one process. Every other outcome —
    unreadable file, empty file, unparsable contents, a value that is not a
    process id — returns `0` *and* a reason, and no caller may probe or signal
    a pid it does not have.

    The last of those four is the one worth stating as a rule rather than as a
    comparison, because `os.kill` does not treat its first argument as an
    identity throughout its range. It is a *selector*, and only one part of
    the range selects a single process:

        pid  > 0    that one process — the only thing a pidfile can record
        pid == 0    every process in the *caller's own* process group
        pid == -1   every process the caller is permitted to signal
        pid  < -1   every process in process group `-pid`

    A pidfile names one daemon. Only a positive value can express that, so
    every non-positive value read out of one is not a pid we disagree with —
    it is a value from a different namespace, and handing it to `os.kill` is a
    broadcast, not a mistargeted kill. `int()` accepts `"0"`, `"+0"` and
    `"-1"` without complaint, so nothing upstream of this rejects them.

    Empty gets its own reason because it has a specific cause worth naming: a
    daemon caught between `open` and `write`.

    This lived in `status.py` (#549), which is the surface that only *reads*.
    `stop.py`, the surface that actually sends signals, had no equivalent and
    called `int(...)` directly — so the guard existed in the one place where
    getting it wrong cost a wrong row, and was missing from the one place
    where it cost a SIGTERM to the caller's process group (#569). It lives
    here now so there is one reader and the two surfaces cannot drift, which
    is what `docs/mcp-integration.md` already promises they will not do.
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
