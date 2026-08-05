#!/usr/bin/env python3
"""MCP daemon — long-lived bridge between a UDS socket and an MCP server subprocess.

Why: spawning cclsp+intelephense per supertool call pays cold-start (30s+) every time.
This daemon keeps the LSP warm. Supertool clients connect via Unix socket and forward
JSON-RPC verbatim.

Usage:
    python3 daemon.py SERVER_NAME           # blocking — serves forever
    python3 daemon.py SERVER_NAME --detach  # double-fork detach

Reads .supertool.json from cwd, looks up mcp[SERVER_NAME] = {cmd, env, timeout, ...}.
Socket/pid paths: per-user runtime dir (#148), via _paths.socket_pid_paths —
hashed sha1(cwd+name)[:12], NOT /tmp.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import select
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

# Shared path helpers (#148): per-user runtime dir, NOT /tmp.
# sys.path manipulation so `_paths` resolves whether daemon.py runs as a
# script (python3 daemon.py — script-dir added automatically) or is imported
# as a module from another cwd (tests — script-dir not in sys.path).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _proc  # noqa: E402  (the one liveness probe — never os.kill(pid, 0), #429)
import _spawn  # noqa: E402  (#451: one daemon per (kind, config fingerprint))
import _paths  # noqa: E402
from _paths import (  # noqa: E402,F401
    open_runtime_dir,
    require_relative_ops,
    socket_pid_names,
    # Re-exported, not used here. Naming these two files *by path* is still how
    # every caller outside this process refers to them — `mcp_status` rows,
    # error messages, tests reaching in for a path to write. What changed is
    # that the daemon itself no longer resolves one (#598), not that paths
    # stopped existing.
    socket_pid_paths,
)

IDLE_TIMEOUT_SEC = 600  # shutdown after 10min idle
ACCEPT_POLL_SEC = 1.0

# SERVER_NAME validation — strict alphanumeric + - _ to keep filesystem paths
# predictable and prevent `..` / slash tricks in the socket-path hash input.
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _validate_name(name: str) -> None:
    if not _NAME_RE.match(name):
        sys.exit(
            f"daemon: invalid server name {name!r} — "
            "must match [A-Za-z0-9_-]{1,64}"
        )


def load_spec(name: str) -> dict:
    """Walk up from cwd looking for .supertool.json; return mcp[name]."""
    d = os.path.abspath(os.getcwd())
    while True:
        p = os.path.join(d, ".supertool.json")
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                cfg = json.load(f)
            spec = (cfg.get("mcp") or {}).get(name)
            if spec is None:
                sys.exit(f"daemon: no mcp.{name} in {p}")
            return spec
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    sys.exit("daemon: no .supertool.json found")


def detach() -> None:
    """Standard double-fork to detach from controlling terminal.

    #148: explicitly reset umask to 0o077 (owner-only) so any files the
    daemon creates after detach (pidfile, log, stderr) inherit owner-only
    perms even if the caller's umask was permissive.
    """
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    os.umask(0o077)
    sys.stdout.flush(); sys.stderr.flush()
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0); os.dup2(devnull, 1); os.dup2(devnull, 2)


def _check_peer_uid(client_sock: socket.socket) -> bool:
    """Return True iff the peer's uid matches our euid (closes #148 C2).

    Without this, any local user with `connect()` access to the UDS could
    speak JSON-RPC to the LSP subprocess running as us. Under the new
    runtime-dir layout (`$XDG_RUNTIME_DIR/supertool/` or `~/Library/Caches/`,
    both mode 0700), parent-dir perms already gate access to our uid — but
    a defence-in-depth check at accept-time costs nothing and catches future
    layout regressions.
    """
    try:
        if sys.platform == "linux":
            import struct
            # SO_PEERCRED — `struct ucred { pid_t pid; uid_t uid; gid_t gid; }`
            data = client_sock.getsockopt(socket.SOL_SOCKET, 17, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", data)
        elif sys.platform == "darwin":
            import struct
            # LOCAL_PEERCRED on macOS — xucred struct, first int is uid version
            # of the simpler form via SO_PEERCRED is not available; use socket.SO_PEERCRED
            # alias if present, else getpeereid via ctypes.
            try:
                so_peercred = socket.SO_PEERCRED  # type: ignore[attr-defined]
                data = client_sock.getsockopt(0, so_peercred, struct.calcsize("3i"))
                _pid, uid, _gid = struct.unpack("3i", data)
            except (AttributeError, OSError):
                # Fallback: macOS getpeereid(2) via ctypes
                import ctypes
                libc = ctypes.CDLL("libc.dylib", use_errno=True)
                uid_p = ctypes.c_uint32()
                gid_p = ctypes.c_uint32()
                r = libc.getpeereid(client_sock.fileno(),
                                    ctypes.byref(uid_p), ctypes.byref(gid_p))
                if r != 0:
                    return True  # fail open if syscall absent — parent-dir perms still gate
                uid = int(uid_p.value)
        else:
            # Other platforms (BSD, etc.): rely on parent-dir perms only.
            return True
        return uid == os.geteuid()
    except (OSError, AttributeError, ImportError):
        # Any failure of the syscall layer — fail open. Parent-dir perms
        # (0700) are the primary boundary; this check is defence in depth.
        return True


def bridge_client(client_sock: socket.socket, proc: subprocess.Popen, last_activity: list, dbg) -> None:
    """Bridge one client connection ↔ subprocess stdio using two blocking threads.

    Simpler than select+non-blocking: each direction is a thread doing blocking reads.
    Either direction closes → set the stop event → both threads exit.
    """
    stop = threading.Event()
    in_fd = proc.stdin.fileno()
    out_fd = proc.stdout.fileno()

    def client_to_proc() -> None:
        try:
            while not stop.is_set():
                data = client_sock.recv(65536)
                if not data:
                    dbg("client→proc: client disconnected"); break
                os.write(in_fd, data)
                last_activity[0] = time.time()
                dbg(f"client→cclsp {len(data)}B")
        except OSError as e:
            dbg(f"client→proc error: {e}")
        finally:
            stop.set()

    def proc_to_client() -> None:
        # select() with timeout lets us check stop event without blocking forever
        # in os.read. Critical: when client disconnects, this thread must exit so the
        # next client's bridge owns the cclsp stdout fd cleanly.
        try:
            while not stop.is_set():
                # Short poll so this thread notices a client disconnect (stop set by
                # client_to_proc) quickly. The daemon serves one client at a time, so a
                # long timeout here makes the NEXT connection wait out this teardown —
                # cheap per-call tools (phpmd ~60ms) end up dominated by it. 50ms keeps
                # teardown tight without meaningful idle-wakeup cost (bridge is short-lived).
                r, _, _ = select.select([out_fd], [], [], 0.05)
                if not r:
                    continue
                try:
                    data = os.read(out_fd, 65536)
                except BlockingIOError:
                    continue
                if not data:
                    dbg("proc→client: cclsp stdout EOF"); break
                client_sock.sendall(data)
                last_activity[0] = time.time()
                dbg(f"cclsp→client {len(data)}B")
        except OSError as e:
            dbg(f"proc→client error: {e}")
        finally:
            stop.set()

    t1 = threading.Thread(target=client_to_proc, daemon=True)
    t2 = threading.Thread(target=proc_to_client, daemon=True)
    t1.start(); t2.start()
    while not stop.is_set():
        if proc.poll() is not None:
            dbg("bridge: subprocess died")
            stop.set(); break
        stop.wait(timeout=1.0)
    try: client_sock.shutdown(socket.SHUT_RDWR)
    except OSError: pass
    t1.join(timeout=2); t2.join(timeout=2)


def claim_pidfile(pid_name: str, *, dir_fd: int) -> bool:
    """Take exclusive ownership of this (cwd, name) slot, or report it taken.

    `O_CREAT|O_EXCL` is the atomic part: exactly one process can create the
    file, so exactly one process is the daemon. A pidfile whose owner is dead
    is removed and the claim retried once — the opposite failure (a crashed
    daemon leftover pidfile wedging every future start) is worse than a
    duplicate, since it leaves the project with no daemon at all.

    `pid_name` is a basename against `dir_fd` (#598). `O_NOFOLLOW` already
    refused a pidfile pre-created as a symlink; what it never covered is the
    *directory* component, so the exclusivity `O_EXCL` establishes used to be
    exclusivity over a path — two daemons resolving that path to two different
    directories would both have "won".

    The read goes through `_paths.read_pid`, which is the one pidfile reader
    (#569): its docstring promises the surfaces cannot drift, and this was a
    fourth reader that had drifted — `_spawn.read_pid` returns a bare int and
    collapses "unreadable", "empty" and "garbage" into `0`, so a pidfile being
    rewritten by a live daemon read as *no owner* and this function unlinked it.
    """
    for _ in range(2):
        try:
            fd = os.open(pid_name,
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=dir_fd)
        except FileExistsError:
            existing, _reason = _paths.read_pid(pid_name, dir_fd=dir_fd)
            if existing and _proc.pid_alive(existing):
                return False
            try:
                os.unlink(pid_name, dir_fd=dir_fd)
            except FileNotFoundError:
                pass
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        return True
    return False


def _bind_in(server: socket.socket, sock_name: str, dir_fd: int) -> None:
    """Bind `sock_name` inside the held directory, by being in it.

    `socket.bind()` takes no `dir_fd` and `socket` offers no descriptor-relative
    form of it, so the only way to name a socket against a descriptor is to make
    that descriptor the cwd. `os.fchdir` is process-global state, which is the
    cost, and it is paid for exactly one call and handed straight back.

    The ordering around it is load-bearing rather than incidental. This runs
    **before** `subprocess.Popen`, so the MCP server child inherits the caller's
    cwd — which is where its project and its config live, and getting that wrong
    would break every language server for a security property none of them care
    about. It also runs **before** the stderr drain thread exists, so no other
    thread can observe the swapped cwd. Moving either above this call would make
    the fchdir visible; they are below it on purpose.

    The alternative shape — `dir_fd` everywhere and a forked child holding the
    bind — buys nothing here, because the window is already single-threaded and
    childless, and costs a fork whose failure modes are worse than the two lines
    it removes.

    A relative `sun_path` also spends none of the ~104-byte (macOS) / 108-byte
    (Linux) `sockaddr_un` budget on the directory. That is not a bonus so much
    as a repair: #583 resolves the configured path before use, and resolving can
    only lengthen it, so a short symlink deliberately pointed at a deep runtime
    dir stopped binding at all.
    """
    prev = os.open(".", os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fchdir(dir_fd)
        server.bind(sock_name)
    finally:
        os.fchdir(prev)
        os.close(prev)
    # Owner-only. A socket is otherwise reachable by anyone who can traverse to
    # it, and the path hash (sha1 of cwd+name) is guessable for known projects.
    # `dir_fd` and not the relative name: this is outside the fchdir window, so
    # a bare name here would resolve against the caller's cwd.
    try:
        os.chmod(sock_name, 0o700, dir_fd=dir_fd)
    except OSError:
        pass


def serve(name: str, spec: dict) -> int:
    """Own the (cwd, name) slot and run the daemon for it.

    The runtime dir is opened **once**, here, and the descriptor is held for the
    whole life of the daemon (#598). Every name below is a basename resolved
    against it, so the directory this process writes into is provably the one
    `_paths` validated for ownership and mode — not a directory that happened to
    be at the same path some seconds later, in another process, after the client
    that spawned us had already let go of its own descriptor.

    Holding a directory fd for hours is cheap but not free: it pins the inode, so
    an operator's `rm -rf` on the runtime dir unlinks the names and leaves the
    daemon writing into a directory that no longer has one. That is the correct
    outcome rather than an accepted cost — the alternative is re-deriving the
    directory mid-flight, which is the behaviour being removed. The space is
    reclaimed when the daemon exits, and `mcp_stop` is the supported way to make
    that happen. Documented in `docs/mcp-integration.md`.
    """
    cwd = os.path.abspath(os.getcwd())
    sock_name, pid_name = socket_pid_names(cwd, name)
    dir_fd, base = open_runtime_dir()
    try:
        require_relative_ops(base)
        sock_path = os.path.join(base, sock_name)
        pid_path = os.path.join(base, pid_name)

        # #451: claim the pidfile BEFORE any side effect. The claim used to
        # happen after the socket rebind and after the MCP server subprocess had
        # been launched, so a daemon that lost the race had already unlinked the
        # incumbent socket path (making the incumbent unreachable) and already
        # spawned a heavy PHP child (which outlived its exiting parent).
        # Claiming first makes losing the race free: notice, say so, touch
        # nothing.
        if not claim_pidfile(pid_name, dir_fd=dir_fd):
            sys.stderr.write(
                f"daemon: {name} already running (pidfile {pid_path}) — "
                "not starting a second\n")
            return 0
        # Record the config this daemon boots with, so a client can tell whether
        # the warm state still matches what is on disk (#451).
        _spawn.write_fingerprint(
            sock_name, _spawn.config_fingerprint(spec, cwd), dir_fd=dir_fd)
        try:
            return _serve_owned(spec, name, sock_name, pid_name, dir_fd, sock_path)
        finally:
            _spawn.cleanup(sock_name, pid_name, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)


def _serve_owned(spec: dict, name: str, sock_name: str, pid_name: str,
                 dir_fd: int, sock_path: str) -> int:
    """Run the daemon. Only ever reached by the process holding the pidfile.

    `name` is the server's key under `mcp` in `.supertool.json`, carried here
    for the one message below that names it. It used to be interpolated
    without being a parameter, so the branch that reports a missing `cmd`
    raised `NameError` instead of printing what was missing (#666).

    `sock_name` / `pid_name` are basenames against `dir_fd`. `sock_path` is
    carried alongside for **messages only** — it is what an operator types, not
    what the kernel is asked for, and the two are allowed to disagree if someone
    moves the directory while we are serving out of it.
    """
    try:
        os.unlink(sock_name, dir_fd=dir_fd)
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    _bind_in(server, sock_name, dir_fd)
    server.listen(8)
    server.settimeout(ACCEPT_POLL_SEC)

    # Spawn MCP server subprocess
    cmd = spec.get("cmd")
    if not cmd:
        sys.exit(f"daemon: mcp.{name}.cmd missing")
    args = spec.get("args") or []
    if isinstance(cmd, str) and not args:
        argv = shlex.split(cmd)
    else:
        argv = [cmd] + list(args) if isinstance(cmd, str) else list(cmd) + list(args)
    env = os.environ.copy()
    if spec.get("env"):
        env.update(spec["env"])
    proc = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )

    # Everything from here on is inside the try whose `finally` reaps `proc`.
    #
    # It did not used to be, and the gap was the two `_safe_open` calls below:
    # they raise `OSError` on exactly the attack they exist to refuse (a
    # `.stderr` pre-created as a symlink → ELOOP), that exception was caught
    # nowhere, and it unwound past a `try` it had never entered. So the #148
    # symlink guard, when it fired, left the heavy MCP server child running with
    # no parent — the stray daemon #451 and `_spawn.py`'s whole docstring exist
    # to prevent. A loud failure that leaks a process is worse than the quiet
    # one it is usually contrasted with, because nothing is left to notice it.
    stderr_log = dbg_log = None
    try:
        # Logs — open with O_NOFOLLOW|O_CREAT (#148), relative to the held
        # runtime dir (#598). If the name is a pre-existing symlink (squatting
        # attack), open() refuses to follow it. Mode 0600 owner-only.
        def _safe_open(name: str, *, mode: int = 0o600) -> int:
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
            return os.open(name, flags, mode, dir_fd=dir_fd)
        stderr_log = os.fdopen(
            _safe_open(f"{sock_name}.stderr"), "ab", buffering=0)
        def drain_stderr() -> None:
            # readline avoids the BufferedReader-on-non-blocking-fd None trap
            while True:
                line = proc.stderr.readline()
                if not line: break
                stderr_log.write(line)
        threading.Thread(target=drain_stderr, daemon=True).start()

        # Daemon debug log — same O_NOFOLLOW guard.
        dbg_log = os.fdopen(_safe_open(f"{sock_name}.log"), "ab", buffering=0)
        def dbg(msg: str) -> None:
            dbg_log.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n".encode())

        # Pidfile: already claimed in serve(), atomically, before this process
        # bound a socket or spawned anything (#148 O_EXCL, #451 ordering).

        # Signal-driven shutdown
        shutting_down = [False]
        def shutdown(*_):
            shutting_down[0] = True
        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)

        last_activity = [time.time()]
        idle_timeout = int(spec.get("idle_timeout", IDLE_TIMEOUT_SEC))

        while not shutting_down[0]:
            # Idle check
            if time.time() - last_activity[0] > idle_timeout:
                break
            # Subprocess died — bail (supervisor will respawn via new client request)
            if proc.poll() is not None:
                break
            try:
                client_sock, _ = server.accept()
            except socket.timeout:
                continue
            # #148 C2: peer-uid check. Rejects any local user whose uid
            # doesn't match ours, defence in depth on top of parent-dir 0700.
            if not _check_peer_uid(client_sock):
                dbg("client rejected — peer uid mismatch")
                try: client_sock.close()
                except OSError: pass
                continue
            dbg("client connected")
            try:
                bridge_client(client_sock, proc, last_activity, dbg)
            finally:
                dbg("client disconnected")
                try:
                    client_sock.close()
                except OSError:
                    pass
    finally:
        # Cleanup. Reached now on *every* exit including a failed log open, so
        # `proc` is reaped whatever went wrong; `stderr_log`/`dbg_log` are
        # guarded because the failure that most needs this finally is the one
        # that happens while opening them.
        try: server.close()
        except OSError: pass
        try: os.unlink(sock_name, dir_fd=dir_fd)
        except OSError: pass
        try: os.unlink(pid_name, dir_fd=dir_fd)
        except OSError: pass
        try: proc.terminate(); proc.wait(timeout=5)
        except Exception:
            try: proc.kill(); proc.wait(timeout=5)
            except Exception: pass
        for handle in (stderr_log, dbg_log):
            if handle is None:
                continue
            try: handle.close()
            except OSError: pass
    return 0


def main(argv: list) -> int:
    if len(argv) < 2:
        sys.stderr.write("usage: daemon.py SERVER_NAME [--detach]\n")
        return 2
    name = argv[1]
    _validate_name(name)
    do_detach = "--detach" in argv[2:]

    spec = load_spec(name)
    cwd = os.path.abspath(os.getcwd())
    sock_name, pid_name = socket_pid_names(cwd, name)

    # Already running? (pidfile + alive process). Advisory only — the
    # authoritative gate is claim_pidfile()'s O_EXCL, under the same descriptor.
    # This exists to avoid a pointless detach-and-spawn, so it is allowed to be
    # wrong in the direction of starting a process that immediately exits.
    #
    # It used to read the pidfile with a bare `open()` + `int()`, which followed
    # symlinks and accepted `0`, `+0` and `-1` — the values `_paths.read_pid`
    # exists to reject (#569), in a file whose docstring promises there is one
    # reader and the surfaces cannot drift. There were three. `_proc.pid_alive`
    # happens to reject non-positive pids too (#429), so nothing was signalled
    # wrongly; the drift is fixed because relying on the next layer's guard is
    # how the first one gets removed by someone who cannot see why it mattered.
    dir_fd, base = open_runtime_dir()
    try:
        existing_pid, _reason = _paths.read_pid(pid_name, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)
    if existing_pid and _proc.pid_alive(existing_pid):
        sock_path = os.path.join(base, sock_name)
        sys.stderr.write(
            f"daemon: already running pid={existing_pid} sock={sock_path}\n")
        return 0
    # A stale pidfile is cleared by claim_pidfile() under O_EXCL — clearing
    # it here too would race with a daemon that claimed it in between (#451).

    if do_detach:
        detach()

    return serve(name, spec)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
