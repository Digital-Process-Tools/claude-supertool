#!/usr/bin/env python3
"""MCP daemon — long-lived bridge between a UDS socket and an MCP server subprocess.

Why: spawning cclsp+intelephense per supertool call pays cold-start (30s+) every time.
This daemon keeps the LSP warm. Supertool clients connect via Unix socket and forward
JSON-RPC verbatim.

Usage:
    python3 daemon.py SERVER_NAME           # blocking — serves forever
    python3 daemon.py SERVER_NAME --detach  # double-fork detach

Reads .supertool.json from cwd, looks up mcp[SERVER_NAME] = {cmd, env, timeout, ...}.
Socket path: /tmp/supertool-mcp-<sha1(cwd+name)[:12]>.sock
Pid file:    /tmp/supertool-mcp-<sha1(cwd+name)[:12]>.pid
"""
from __future__ import annotations

import hashlib
import json
import os
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

IDLE_TIMEOUT_SEC = 600  # shutdown after 10min idle
ACCEPT_POLL_SEC = 1.0


def socket_pid_paths(cwd: str, name: str) -> Tuple[str, str]:
    h = hashlib.sha1(f"{cwd}::{name}".encode()).hexdigest()[:12]
    base = f"/tmp/supertool-mcp-{h}"
    return f"{base}.sock", f"{base}.pid"


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
    """Standard double-fork to detach from controlling terminal."""
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    sys.stdout.flush(); sys.stderr.flush()
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0); os.dup2(devnull, 1); os.dup2(devnull, 2)


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

    # Drain subprocess stderr to /tmp/supertool-mcp-<hash>.stderr (debug aid)
    stderr_log = open(f"{sock_path}.stderr", "ab", buffering=0)
    stderr_fd = proc.stderr.fileno()
    def drain_stderr() -> None:
        # readline avoids the BufferedReader-on-non-blocking-fd None trap
        while True:
            line = proc.stderr.readline()
            if not line: break
            stderr_log.write(line)
    threading.Thread(target=drain_stderr, daemon=True).start()

    # Daemon debug log
    dbg_log = open(f"{sock_path}.log", "ab", buffering=0)
    def dbg(msg: str) -> None:
        dbg_log.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n".encode())

    # Write pidfile
    with open(pid_path, "w") as f:
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
