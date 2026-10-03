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
and named.

That guarantee used to stop at the daemon, which is a separate process handed
strings and re-resolved every one of them (#598). It no longer does: the daemon
calls `open_runtime_dir()` itself and holds the descriptor for its whole life,
so its pidfile, logs, fingerprint and unlinks are `dir_fd`-relative and its
socket is bound by basename from inside the directory. `socket.bind()` still
takes no `dir_fd` — there is no descriptor-relative form of it in `socket` — so
the one place a name is resolved against the process cwd is a two-call window
under `os.fchdir`, before any thread or subprocess exists to observe it. See
`docs/mcp-integration.md`.
"""
from __future__ import annotations

import errno
import hashlib
import os
import stat
import sys
from pathlib import Path
from typing import Optional, Tuple


def _runtime_base() -> Path:
    """The configured runtime dir path, before any of it has been verified.

    Order of preference:
      1. `$SUPERTOOL_RUNTIME_DIR` (explicit override — tests, custom setups)
      2. `$XDG_RUNTIME_DIR/supertool/mcp/` (Linux / freedesktop spec)
      3. `~/Library/Caches/supertool/mcp/` (macOS)
      4. `~/.cache/supertool/mcp/` (fallback)

    Both environment variables must be **absolute** (#607). A relative value is
    resolved against the current working directory, which would put the daemon
    socket and pidfile inside whichever project supertool happens to be invoked
    from — a per-cwd runtime dir, with that project's ancestry rather than the
    user's. The freedesktop spec requires `$XDG_RUNTIME_DIR` to be absolute for
    exactly this reason. Refused rather than silently rewritten: guessing that
    `runtime/mcp` meant `$HOME/runtime/mcp` would relocate a daemon location on
    the operator's behalf, which is the quiet failure this module rejects.
    """
    override = os.environ.get("SUPERTOOL_RUNTIME_DIR")
    if override:
        _require_absolute(override, "SUPERTOOL_RUNTIME_DIR")
        return Path(override)
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        _require_absolute(xdg, "XDG_RUNTIME_DIR")
    if xdg and Path(xdg).is_dir():
        return Path(xdg) / "supertool" / "mcp"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "supertool" / "mcp"
    return Path.home() / ".cache" / "supertool" / "mcp"


def _require_absolute(value: str, var: str) -> None:
    """Refuse a relative runtime-dir setting rather than resolving it (#607)."""
    if os.path.isabs(value):
        return
    sys.exit(
        f"daemon: {var} is set to {value!r}, which is not an absolute path. A "
        f"relative runtime dir is resolved against the current working "
        f"directory, so the daemon socket and pidfile would land inside "
        f"whichever project supertool was invoked from — a different directory "
        f"per invocation, with that project's owners rather than yours. "
        f"Refusing rather than guessing what it was meant to be relative to. "
        f"Set {var} to an absolute path."
    )


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

# The second half of the same story (#598). Holding the directory open is only
# worth anything if the things inside it can be named *relative to* the
# descriptor; otherwise a validated fd sits beside a path that still gets
# re-resolved. `os.supports_dir_fd` is a runtime probe of the `*at` syscalls —
# `openat`, `unlinkat`, `fchmodat`, `renameat` — and Windows has none of them.
#
# Probed at import for the same reason as `_LISTDIR_TAKES_FD` above: the sets
# hold the original function objects, so asking after a test has wrapped one
# asks whoever patched last and turns a test double into a platform verdict.
# The ancestry walk (#607) climbs by `os.open("..", dir_fd=<held fd>)` rather
# than by re-resolving each parent path, so that every component it judges is
# reached through the descriptor chain that starts at the directory already
# validated. `os.open` with `dir_fd` is POSIX-only; Windows has no `openat`.
#
# Probed at import for the same reason as everything else in this block: the
# support sets hold the original function objects, so asking after a test has
# wrapped `os.open` — which one below does, to simulate an unreadable ancestor
# — would report a test double as a missing syscall.
_ANCESTRY_DIR_FD = os.open in os.supports_dir_fd

_RELATIVE_OPS = {
    "os.open(dir_fd=)": os.open in os.supports_dir_fd,
    "os.unlink(dir_fd=)": os.unlink in os.supports_dir_fd,
    "os.chmod(dir_fd=)": os.chmod in os.supports_dir_fd,
    "os.rename(dir_fd=)": os.rename in os.supports_dir_fd,
    "os.fchdir": hasattr(os, "fchdir"),
}


def require_relative_ops(base: str) -> None:
    """Refuse where the daemon cannot name files relative to the held fd (#598).

    A *decline*, not a finding, and worded as one: the question "did anything
    move between validation and use?" is unanswerable without the `*at`
    syscalls, and #544's rule is that the check which cannot ask its question is
    the one that has to say so. Falling back to path-based opens here would be
    the quiet failure — it would look like hardening, behave like the code this
    replaces, and report `ok`.

    Unreachable on every platform CPython currently ships: the absence of these
    coincides with the absence of `O_DIRECTORY`/`O_NOFOLLOW`, so `_require_dir_fd`
    refuses first and `os.geteuid` before that. Written independently anyway,
    for the same reason that one is — a guard that is correct only because
    another guard happens to run earlier is a comment pretending to be code.
    """
    missing = [name for name, ok in _RELATIVE_OPS.items() if not ok]
    if not missing:
        return
    sys.exit(
        f"daemon: cannot open files relative to the runtime dir {base} on this "
        f"platform — {', '.join(missing)} unavailable. The directory can be "
        f"validated here but not held to, so every file the daemon creates "
        f"would re-resolve the path and could land in a directory nothing "
        f"inspected (#598). That question cannot be asked here rather than "
        f"merely being awkward, so this declines instead of writing to "
        f"whatever the path currently names. Run the daemon on a platform "
        f"with POSIX *at syscalls."
    )


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


def open_runtime_dir() -> Tuple[int, str]:
    """The validated runtime dir as `(fd, resolved_path)`; caller closes `fd`.

    The public form of `_open_runtime_dir`, for callers that can work
    descriptor-relative and therefore should (#598): `list_pidfiles` here, and
    `daemon.py`, which holds it for the daemon's whole life.

    Prefer this over `runtime_dir()` wherever the answer is going to be used to
    open something. `runtime_dir()` closes the descriptor and hands back a
    string, which re-opens the exact gap the descriptor exists to close — it
    remains only for callers that genuinely need a path to *print*.
    """
    return _open_runtime_dir()


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
        _exit_loose_mode(where, mode, resolved)
    _verify_ancestry(fd, resolved, geteuid)


def _exit_loose_mode(where: str, mode: int, resolved: str) -> None:
    """The #568 refusal, lifted out so `_verify_runtime_dir` stays readable."""
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


def _ancestor_finding(st: os.stat_result, geteuid) -> str:
    """Why this ancestor is a lever, or `""` if it is not (#607).

    Two rules, and they are **independent** rather than alternatives:

    - **Owner must be us or root.** A directory someone else owns is theirs to
      rearrange. `root` is allowed because `/`, `/run`, `/run/user`, `/Users`
      and `/home` are all root-owned on every healthy machine — a rule that
      refused those would refuse every installation there is.
    - **Not group- or world-writable, unless sticky.** `ssh`'s `StrictModes`
      checks `& 022` and this is the same question. The sticky exception is
      what makes `/tmp` (`1777`) usable: with `S_ISVTX` set, only an entry's
      owner — or the directory's owner, or root — may rename or unlink it.

    The sticky bit does **not** rescue a stranger-owned directory, which is why
    the ownership rule is checked first and separately: a `1777` directory
    belonging to someone else still lets its owner remove any entry in it.
    """
    mode = stat.S_IMODE(st.st_mode)
    if st.st_uid not in (geteuid(), 0):
        return (
            f"owned by uid {st.st_uid}, not us ({geteuid()}) or root — its "
            f"owner may rename or remove any entry in it, including ours"
        )
    if mode & 0o022 and not mode & stat.S_ISVTX:
        who = "world-writable" if mode & 0o002 else "group-writable"
        return (
            f"{oct(mode)} — {who} with no sticky bit, so any user who can "
            f"write it may rename() our runtime dir out of it and put their "
            f"own directory in its place"
        )
    return ""


def _verify_ancestry(fd: int, resolved: str, geteuid) -> None:
    """Walk from the validated directory up to the root, checking each parent.

    The leaf's own `0700` is not what gates a replacement, and three issues
    (#568, #583, #598) each concluded it was. POSIX permits `rename()` over any
    entry in a writable, non-sticky directory *regardless of that entry's own
    owner or mode* — so someone who can write any ancestor can substitute a
    directory of their own for ours, and every check made about the leaf goes
    on passing about an object nobody is using. `$XDG_RUNTIME_DIR` was accepted
    on the sole evidence that it `is_dir()`, so this was reachable cross-uid.

    **How far up:** to the filesystem root, mount points included. Crossing one
    is not a stopping condition — `..` at a mount root is handed to the parent
    filesystem by the kernel, and every process that later re-resolves the
    returned string (`stop.py`, `status.py`, the daemon on its next boot)
    traverses those components too, so they are part of the path's trust chain
    whether or not a `rename()` could reach across the boundary.

    **What it does when it fails:** refuses, with a sentence, the way the
    ownership (#544) and mode (#568) checks beside it do. It does not relocate.
    A fallback to a private directory supertool creates itself would move every
    warm daemon out from under the clients still connecting to the old socket
    path — a quiet failure swapped in for a loud one, which is the trade this
    module exists to refuse.

    **What it does when it cannot run:** says so, in different words. A
    platform without `openat` cannot ask the question, and a component that
    will not open leaves it unanswered; neither is "no finding". Same
    three-state contract as `docs/validators.md`, same vocabulary as
    `_require_dir_fd` and `require_relative_ops` above — the pattern was
    already here, so nothing new was invented for it (#263).
    """
    if not _ANCESTRY_DIR_FD:
        sys.exit(
            f"daemon: cannot check who owns the directories above {resolved} on "
            f"this platform — os.open(dir_fd=) is unavailable, so the walk from "
            f"the validated directory up to the root cannot be made through "
            f"descriptors. A writable ancestor lets any user who can write it "
            f"replace the runtime dir wholesale, whatever the runtime dir's own "
            f"mode says (#607), and that question cannot be asked here rather "
            f"than merely being awkward. Refusing instead of assuming the "
            f"ancestry is sound."
        )
    # Names for the message only. The *checks* below are made through the
    # descriptor chain, so a concurrent rename cannot make us judge one
    # directory and report another; the worst it can do is make a refusal name
    # a stale path, which is a worse sentence rather than a wrong verdict.
    names = [str(p) for p in Path(resolved).parents]
    child = os.dup(fd)
    try:
        for step, name in enumerate(names):
            try:
                parent = os.open("..", os.O_RDONLY | os.O_DIRECTORY, dir_fd=child)
            except OSError as exc:
                if step == 0 and exc.errno in (errno.EACCES, errno.EPERM):
                    # The runtime dir itself has no search bit — `0o600` is the
                    # reachable shape, since #568 accepts it (owner-only, so
                    # nothing is exposed) and a filesystem where the tightening
                    # chmod is a no-op can leave it there. `..` cannot be opened
                    # from a directory you cannot search, so the walk has no
                    # starting point. Named separately because "fix the mode of
                    # the runtime dir" and "fix the mode of something above it"
                    # send an operator to different places.
                    sys.exit(
                        f"daemon: runtime dir {resolved} has no search "
                        f"permission for its owner, so the directories above it "
                        f"cannot be walked and it is unknowable whether a "
                        f"stranger could replace it (#607). The daemon could not "
                        f"open anything inside it either. Fix it with "
                        f"`chmod 700 {resolved}`."
                    )
                sys.exit(
                    f"daemon: could not open {name}, an ancestor of the runtime "
                    f"dir {resolved}: {exc.strerror or exc}. Whether a stranger "
                    f"can replace the runtime dir depends on who owns the "
                    f"directories above it (#607), and that question is now "
                    f"unanswered rather than answered favourably. Refusing. "
                    f"Set SUPERTOOL_RUNTIME_DIR to an absolute path whose "
                    f"parents you can read."
                )
            os.close(child)
            child = parent
            try:
                st = os.fstat(child)
            except OSError as exc:
                sys.exit(
                    f"daemon: could not stat {name}, an ancestor of the runtime "
                    f"dir {resolved}: {exc.strerror or exc}. Refusing rather "
                    f"than treating an unasked question as a clean answer "
                    f"(#607)."
                )
            finding = _ancestor_finding(st, geteuid)
            if finding:
                sys.exit(
                    f"daemon: {name} is {finding}. It is an ancestor of the "
                    f"runtime dir {resolved}, and the runtime dir's own 0700 is "
                    f"no defence: POSIX allows any entry in a writable, "
                    f"non-sticky directory to be renamed away by whoever can "
                    f"write that directory, whatever the entry itself is set to "
                    f"(#607). The daemon socket and pidfiles would then live in "
                    f"a directory nothing inspected. Refusing to use it — and "
                    f"deliberately not relocating, which would move every warm "
                    f"daemon out from under the clients still looking for it. "
                    f"Fix it with `chmod go-w {name}` (or `chmod 755 {name}`), "
                    f"or set SUPERTOOL_RUNTIME_DIR to an absolute path whose "
                    f"every parent is yours — `/run/user/{geteuid()}` on Linux."
                )
    finally:
        os.close(child)


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
    sock_name, pid_name = socket_pid_names(cwd, name)
    base = runtime_dir()
    return os.path.join(base, sock_name), os.path.join(base, pid_name)


def socket_pid_names(cwd: str, name: str) -> Tuple[str, str]:
    """The same two files, as basenames relative to the runtime dir (#598).

    Deliberately does **not** call `runtime_dir()`: a caller holding the
    validated descriptor wants a name to resolve against it, and joining a path
    on first would hand back something that resolves against the filesystem
    instead. The hash is the only thing the caller cannot compute for itself,
    so it is the only thing this returns.
    """
    h = hashlib.sha1(f"{cwd}::{name}".encode()).hexdigest()[:12]
    return f"supertool-mcp-{h}.sock", f"supertool-mcp-{h}.pid"


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

def read_pid(pid_path: str, *, dir_fd: Optional[int] = None) -> Tuple[int, str]:
    """Return `(pid, reason)`; a non-empty reason means the pid is unknowable.

    With `dir_fd`, `pid_path` is a basename resolved against that descriptor and
    the open carries `O_NOFOLLOW` — the reader then provably reads the pidfile
    in the directory that was validated, which is what the daemon and `stop.py`
    both want (#598). Without it the read is by path, for callers that only
    have one.

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
        if dir_fd is None:
            raw = Path(pid_path).read_text(encoding="utf-8").strip()
        else:
            fd = os.open(pid_path, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
            with os.fdopen(fd, encoding="utf-8") as f:
                raw = f.read().strip()
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
