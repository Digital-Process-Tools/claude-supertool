










































from __future__ import annotations

import errno
import hashlib
import os
import stat
import sys
from pathlib import Path
from typing import Optional, Tuple


def _runtime_base() -> Path:

















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














_DIR_FD_FLAGS = ("O_DIRECTORY", "O_NOFOLLOW")






_LISTDIR_TAKES_FD = os.listdir in os.supports_fd



















_ANCESTRY_DIR_FD = os.open in os.supports_dir_fd

_RELATIVE_OPS = {
    "os.open(dir_fd=)": os.open in os.supports_dir_fd,
    "os.unlink(dir_fd=)": os.unlink in os.supports_dir_fd,
    "os.chmod(dir_fd=)": os.chmod in os.supports_dir_fd,
    "os.rename(dir_fd=)": os.rename in os.supports_dir_fd,
    "os.fchdir": hasattr(os, "fchdir"),
}


def require_relative_ops(base: str) -> None:















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












    fd, path = _open_runtime_dir()
    os.close(fd)
    return path


def open_runtime_dir() -> Tuple[int, str]:











    return _open_runtime_dir()


def _open_runtime_dir() -> Tuple[int, str]:
















    base = _runtime_base()












    try:
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        sys.exit(
            f"daemon: cannot create runtime dir {base}: "
            f"{exc.strerror or exc}. Set SUPERTOOL_RUNTIME_DIR to a path you "
            f"can create as a directory."
        )














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





    try:
        _verify_runtime_dir(fd, resolved, base, geteuid)
    except BaseException:
        os.close(fd)
        raise
    return fd, resolved


def _verify_runtime_dir(fd: int, resolved: str, base: Path, geteuid) -> None:















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















    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o077:
        _exit_loose_mode(where, mode, resolved)
    _verify_ancestry(fd, resolved, geteuid)


def _exit_loose_mode(where: str, mode: int, resolved: str) -> None:

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




    names = [str(p) for p in Path(resolved).parents]
    child = os.dup(fd)
    try:
        for step, name in enumerate(names):
            try:
                parent = os.open("..", os.O_RDONLY | os.O_DIRECTORY, dir_fd=child)
            except OSError as exc:
                if step == 0 and exc.errno in (errno.EACCES, errno.EPERM):








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








    if resolved == str(base):
        return resolved
    return f"{resolved} (reached via {base})"


def socket_pid_paths(cwd: str, name: str) -> Tuple[str, str]:





    sock_name, pid_name = socket_pid_names(cwd, name)
    base = runtime_dir()
    return os.path.join(base, sock_name), os.path.join(base, pid_name)


def socket_pid_names(cwd: str, name: str) -> Tuple[str, str]:








    h = hashlib.sha1(f"{cwd}::{name}".encode()).hexdigest()[:12]
    return f"supertool-mcp-{h}.sock", f"supertool-mcp-{h}.pid"


def list_pidfiles() -> Tuple[list, str]:
























    fd, base = _open_runtime_dir()
    try:






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
