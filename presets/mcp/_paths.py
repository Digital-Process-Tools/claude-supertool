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
fallback) created mode `0700` and ownership-checked. The opens of the files
*inside* that directory use `O_NOFOLLOW | O_CREAT | O_EXCL`, so a pidfile or a
`.stderr` log pre-created as a symlink is rejected rather than followed.

`O_NOFOLLOW` on those opens says nothing about the directory holding them, and
that used to be the gap (#583): the directory was reached by path for the
`mkdir`, again for the `chmod`, again for each `stat`, and again by every caller
of `runtime_dir()`, with `chmod` and `stat` both following symlinks. The
directory is now resolved once and held open as an `O_DIRECTORY | O_NOFOLLOW`
descriptor for the whole of its validation — `fchmod`/`fstat`, not `chmod`/`stat`
— and the path handed back to callers is the resolved, symlink-free one. A
symlink is still a legitimate thing to point `SUPERTOOL_RUNTIME_DIR` at; it is
resolved deliberately, once, and the object on the far end is what gets checked
and named. See `docs/mcp-integration.md` for where that guarantee stops:
`socket.bind()` takes no `dir_fd`, so the daemon's own final open is still by
path — a path that no longer traverses a link.
"""
from __future__ import annotations

import hashlib
import os
import stat
import sys
from pathlib import Path
from typing import Tuple


def _runtime_base() -> Path:
    """The configured runtime dir path, before any of it has been verified.

    Order of preference:
      1. `$SUPERTOOL_RUNTIME_DIR` (explicit override — tests, custom setups)
      2. `$XDG_RUNTIME_DIR/supertool/mcp/` (Linux / freedesktop spec)
      3. `~/Library/Caches/supertool/mcp/` (macOS)
      4. `~/.cache/supertool/mcp/` (fallback)
    """
    override = os.environ.get("SUPERTOOL_RUNTIME_DIR")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg and Path(xdg).is_dir():
        return Path(xdg) / "supertool" / "mcp"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "supertool" / "mcp"
    return Path.home() / ".cache" / "supertool" / "mcp"


# The flags that make a directory holdable rather than merely reachable. Both
# are POSIX; neither exists on Windows, where CPython gates them on the C
# macro. `os.listdir(fd)` is the same story via `posix.fdlistdir`.
#
# So the descriptor path below is POSIX-only *by construction*, and on every
# platform CPython ships the absence of these coincides with the absence of
# `os.geteuid` — meaning #544's ownership refusal fires first and this one is
# unreachable there. The guard is still written independently rather than
# resting on that coincidence: the rule is that the check which cannot ask its
# question is the one that has to say so, and a capability guard that is
# correct only because another guard happens to run earlier is a comment
# pretending to be code.
_DIR_FD_FLAGS = ("O_DIRECTORY", "O_NOFOLLOW")

# Probed at import, not per call, and deliberately so: `os.supports_fd` holds the
# *original* function objects, so the membership test stops meaning anything the
# moment anyone wraps `os.listdir` — which tests do, to simulate an unreadable
# runtime dir (#551). Asking at import asks the interpreter; asking later asks
# whoever patched last, and would turn a test double into a platform verdict.
_LISTDIR_TAKES_FD = os.listdir in os.supports_fd


def _require_dir_fd(base: Path) -> None:
    """Refuse where the runtime dir cannot be pinned to a descriptor at all."""
    missing = [name for name in _DIR_FD_FLAGS if not hasattr(os, name)]
    if not _LISTDIR_TAKES_FD:
        missing.append("os.listdir(fd)")
    if not missing:
        return
    sys.exit(
        f"daemon: cannot pin runtime dir {base} to a directory descriptor on "
        f"this platform — {', '.join(missing)} unavailable. Without it every "
        f"check and every use would re-resolve the path independently, and a "
        f"symlink swapped in between would send the daemon socket into a "
        f"directory nothing inspected (#583). That question cannot be asked "
        f"here rather than merely being awkward, so this declines instead of "
        f"checking whatever the path currently points at. Set "
        f"SUPERTOOL_RUNTIME_DIR to a directory on a platform with POSIX "
        f"directory descriptors."
    )


def runtime_dir() -> str:
    """Return the per-user runtime directory for supertool MCP daemons.

    Creates it mode `0700` if missing, then validates it against a held
    descriptor and returns the **resolved, symlink-free** path. Refuses — with
    a sentence, never a traceback — when it is owned by another uid, when it is
    not owner-only and cannot be made so (#568), when it cannot be created, or
    when this platform cannot hold a directory open at all (#583).

    The returned string is what the daemon, `stop.py` and `status.py` re-resolve
    later, in other processes; `_open_runtime_dir` is the version to call when
    the descriptor itself is usable.
    """
    fd, path = _open_runtime_dir()
    os.close(fd)
    return path


def _open_runtime_dir() -> Tuple[int, str]:
    """Return `(fd, resolved_path)` for the validated runtime dir.

    The caller owns the descriptor and must close it. `fd` and `resolved_path`
    name the same object at the moment this returns, which is the whole point:
    every question asked below is asked of `fd`, so no two answers can be about
    two different directories (#583).

    Before, the directory was reached by path four times over — `mkdir`,
    `chmod`, `stat` for ownership, `stat` for mode — and both `chmod` and `stat`
    follow symlinks. Reading through a link is not itself a wrong answer: the
    link's target is what a later `bind` would use, so describing the target is
    describing the right object. What was missing was anything tying the four
    resolutions to each other, or to the fifth one a caller performs on the
    returned string. Repointing the link between any two of them left every
    check passing about a directory that was no longer the one in use.
    """
    base = _runtime_base()
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
    _require_dir_fd(base)

    # Resolve the configured path once, on purpose, and then never again.
    #
    # A symlink here is not an attack — pointing SUPERTOOL_RUNTIME_DIR at one is
    # a reasonable thing to have done deliberately, and banning it would break a
    # working setup to fix a problem it is not the cause of. So the link is
    # followed exactly once, by us, and the object on the far end is what gets
    # opened, checked, named in every message, and handed back.
    #
    # `O_NOFOLLOW` on the open of the *resolved* path is therefore not a symlink
    # ban either: a resolved path's leaf is by definition not a link, so the flag
    # can only fire if the path changed shape between the resolve and the open —
    # a swap in exactly the window this is closing. That gets a refusal rather
    # than a second, following attempt, since re-resolving on failure is the
    # behaviour being removed.
    resolved = os.path.realpath(base)
    try:
        fd = os.open(resolved, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        sys.exit(
            f"daemon: cannot hold runtime dir {resolved} open as a directory: "
            f"{exc.strerror or exc}. Either it stopped being a directory while "
            f"we were looking at it — a symlink or a file swapped in between "
            f"the resolve and the open, which is the race this refuses to run "
            f"(#583) — or it is not usable as one. Refusing to fall back to an "
            f"open that follows links. Point SUPERTOOL_RUNTIME_DIR at a "
            f"directory on a path only you can write."
        )

    # From here on `fd` is the directory. Every failure closes it and re-raises,
    # SystemExit included, so a refusal does not leak a descriptor into a
    # long-lived caller (supertool.py's MCP client turns these into
    # MCPServerError and carries on down the cold path, #582).
    try:
        _verify_runtime_dir(fd, resolved, base, geteuid)
    except BaseException:
        os.close(fd)
        raise
    return fd, resolved


def _verify_runtime_dir(fd: int, resolved: str, base: Path, geteuid) -> None:
    """Tighten and check the held directory — ownership (#544) and mode (#568).

    Both answers come from a single `fstat` on `fd`, so they cannot disagree
    with each other or with the directory the caller goes on to use.
    """
    # The chmod still has a job. `mode=` on the mkdir applies only to the leaf
    # and only when we are the ones creating it: it is ignored for a directory
    # that already exists, and ignored for the intermediate directories
    # `parents=True` makes. Its result is checked below rather than assumed —
    # the `except OSError: pass` that used to be the whole of the handling
    # left the requirement stated in a comment and enforced by nothing.
    #
    # `fchmod` rather than `chmod`: a path-based chmod on a symlinked runtime
    # dir tightens whatever the link points at *now*, which is not necessarily
    # what the stat below will describe.
    try:
        os.fchmod(fd, 0o700)
    except OSError:
        pass
    where = _describe(resolved, base)
    try:
        st = os.fstat(fd)
    except OSError as e:
        sys.exit(f"daemon: cannot stat {where}: {e}")
    if st.st_uid != geteuid():
        sys.exit(
            f"daemon: runtime dir {where} owned by uid {st.st_uid}, "
            f"not us ({geteuid()}). Refusing to use it. "
            f"Set SUPERTOOL_RUNTIME_DIR to a directory you own."
        )
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
            f"daemon: runtime dir {where} is {oct(mode)}, not owner-only, and "
            f"the chmod to 0700 did not take. On Linux it is this directory's "
            f"mode that gates a co-tenant's connect() to the daemon socket and "
            f"their enumeration of the pidfiles (#148), so this is exposure "
            f"rather than untidiness. Refusing to use it. Fix it with "
            f"`chmod 700 {resolved}` — or, if this is a filesystem with no POSIX "
            f"modes (exFAT/FAT32/SMB), remount it with `umask=077` or point "
            f"SUPERTOOL_RUNTIME_DIR at a filesystem that has them."
        )


def _describe(resolved: str, base: Path) -> str:
    """Name the directory a message is about, and how it was reached.

    The resolved path first, always: it is the one to `chmod`, the one to
    `chown`, the one the socket lands in. The configured path is mentioned only
    when the two differ, because that is when an operator staring at their own
    `SUPERTOOL_RUNTIME_DIR` would otherwise not recognise the directory being
    complained about.
    """
    if resolved == str(base):
        return resolved
    return f"{resolved} (reached via {base})"


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
    fd, base = _open_runtime_dir()
    try:
        # `os.listdir(fd)` and not `os.listdir(base)`: this is the one consumer
        # that can be handed the validated directory itself instead of a path to
        # re-resolve, so the names below provably come out of the directory the
        # checks just passed on (#583). The joined paths still go to stop.py as
        # strings — see docs/mcp-integration.md for where that guarantee ends —
        # but `base` is now symlink-free, so nothing can re-aim them.
        names = os.listdir(fd)
    except OSError as exc:
        return [], f"cannot list runtime dir {base}: {exc.strerror or exc}"
    finally:
        os.close(fd)
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
