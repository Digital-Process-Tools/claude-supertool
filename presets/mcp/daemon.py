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
from _paths import socket_pid_paths  # noqa: E402

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
            with open(p) as f:
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
                r, _, _ = select.select([out_fd], [], [], 0.5)
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


def serve(name: str, spec: dict) -> int:
    cwd = os.path.abspath(os.getcwd())
    sock_path, pid_path = socket_pid_paths(cwd, name)

    # Bind socket first (before fork-after-spawn races)
    try:
        os.unlink(sock_path)
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    # Tighten perms: owner-only. Sockets in /tmp are otherwise world-accessible,
    # and the socket-path hash (sha1 of cwd+name) is guessable for known projects.
    try: os.chmod(sock_path, 0o700)
    except OSError: pass
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

    # Drain subprocess stderr — open with O_NOFOLLOW|O_CREAT (#148). If the
    # path is a pre-existing symlink (squatting attack), open() refuses to
    # follow it. Mode 0600 owner-only.
    def _safe_open(path: str, *, mode: int = 0o600) -> int:
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
        return os.open(path, flags, mode)
    stderr_log = os.fdopen(_safe_open(f"{sock_path}.stderr"), "ab", buffering=0)
    stderr_fd = proc.stderr.fileno()
    def drain_stderr() -> None:
        # readline avoids the BufferedReader-on-non-blocking-fd None trap
        while True:
            line = proc.stderr.readline()
            if not line: break
            stderr_log.write(line)
    threading.Thread(target=drain_stderr, daemon=True).start()

    # Daemon debug log — same O_NOFOLLOW guard.
    dbg_log = os.fdopen(_safe_open(f"{sock_path}.log"), "ab", buffering=0)
    def dbg(msg: str) -> None:
        dbg_log.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n".encode())

    # Write pidfile via O_CREAT|O_EXCL|O_NOFOLLOW (#148) — refuses to overwrite
    # an existing pidfile (race window between main()'s liveness check and here).
    try:
        pid_fd = os.open(pid_path,
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600)
    except FileExistsError:
        sys.exit(f"daemon: pidfile {pid_path} already exists — race with another start?")
    with os.fdopen(pid_fd, "w") as f:
        f.write(str(os.getpid()))

    # Signal-driven shutdown
    shutting_down = [False]
    def shutdown(*_):
        shutting_down[0] = True
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    last_activity = [time.time()]
    idle_timeout = int(spec.get("idle_timeout", IDLE_TIMEOUT_SEC))

    try:
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
        # Cleanup
        try: server.close()
        except OSError: pass
        try: os.unlink(sock_path)
        except FileNotFoundError: pass
        try: os.unlink(pid_path)
        except FileNotFoundError: pass
        try: proc.terminate(); proc.wait(timeout=5)
        except Exception:
            try: proc.kill()
            except Exception: pass
        try: stderr_log.close()
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
    sock_path, pid_path = socket_pid_paths(cwd, name)

    # Already running? (pidfile + alive process)
    if os.path.exists(pid_path):
        try:
            with open(pid_path) as f:
                existing_pid = int(f.read().strip())
            os.kill(existing_pid, 0)  # check alive
            sys.stderr.write(f"daemon: already running pid={existing_pid} sock={sock_path}\n")
            return 0
        except (ProcessLookupError, ValueError):
            try: os.unlink(pid_path)
            except FileNotFoundError: pass

    if do_detach:
        detach()

    return serve(name, spec)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
