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
    we will not trust a directory we don't own.
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

    base.mkdir(parents=True, exist_ok=True)
    # Tighten perms — directory must be owner-only.
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
    ownership, which leaves an `ENOENT` race against its own `mkdir`, `EIO` on a
    failing volume, and `EMFILE` under fd pressure. Fixed anyway because the
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
