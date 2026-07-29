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
    # Where `os.geteuid` does not exist (Windows) the comparison is not merely
    # unavailable, it is unanswerable: `st_uid` is a constant 0 there and
    # carries no information about who owns the directory. So this refuses
    # rather than waving the check through (#531). Defaulting it to "ours"
    # would trade a loud failure for a quiet one on the single check whose
    # whole job is to be suspicious, and a security check that silently stops
    # running is indistinguishable from one that keeps passing.
    #
    # Nothing reaches here on such a platform today — the adapters decline
    # earlier, for want of AF_UNIX — but this function is also called by
    # stop.py and status.py, and the next caller should meet a sentence rather
    # than an AttributeError.
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        sys.exit(
            f"daemon: cannot verify ownership of runtime dir {base} on this "
            f"platform (no os.geteuid; st_uid is not meaningful here). "
            f"Refusing to use it. Set SUPERTOOL_RUNTIME_DIR to a directory "
            f"you own on a platform where ownership can be checked."
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


def list_pidfiles() -> list:
    """Return all `supertool-mcp-*.pid` paths in the runtime dir.

    Used by status.py / stop.py to enumerate daemons.
    """
    base = runtime_dir()
    try:
        return sorted(
            os.path.join(base, name)
            for name in os.listdir(base)
            if name.startswith("supertool-mcp-") and name.endswith(".pid")
        )
    except OSError:
        return []
