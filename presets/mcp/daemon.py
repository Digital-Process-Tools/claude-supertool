#!/usr/bin/env python3














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









os.environ.pop("FORCE_COLOR", None)





sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _proc  
import _spawn  
import _paths  
from _paths import (  
    open_runtime_dir,
    require_relative_ops,
    socket_pid_names,





    socket_pid_paths,
)

IDLE_TIMEOUT_SEC = 600  
ACCEPT_POLL_SEC = 1.0



_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}\Z")  


def _validate_name(name: str) -> None:
    if not _NAME_RE.match(name):
        sys.exit(
            f"daemon: invalid server name {name!r} — "
            "must match [A-Za-z0-9_-]{1,64}"
        )


def load_spec(name: str) -> dict:

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









    try:
        if sys.platform == "linux":
            import struct

            data = client_sock.getsockopt(socket.SOL_SOCKET, 17, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", data)
        elif sys.platform == "darwin":
            import struct



            try:
                so_peercred = socket.SO_PEERCRED  
                data = client_sock.getsockopt(0, so_peercred, struct.calcsize("3i"))
                _pid, uid, _gid = struct.unpack("3i", data)
            except (AttributeError, OSError):

                import ctypes
                libc = ctypes.CDLL("libc.dylib", use_errno=True)
                uid_p = ctypes.c_uint32()
                gid_p = ctypes.c_uint32()
                r = libc.getpeereid(client_sock.fileno(),
                                    ctypes.byref(uid_p), ctypes.byref(gid_p))
                if r != 0:
                    return True  
                uid = int(uid_p.value)
        else:

            return True
        return uid == os.geteuid()
    except (OSError, AttributeError, ImportError):


        return True


def bridge_client(client_sock: socket.socket, proc: subprocess.Popen, last_activity: list, dbg) -> None:





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



        try:
            while not stop.is_set():





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


























    prev = os.open(".", os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fchdir(dir_fd)
        server.bind(sock_name)
    finally:
        os.fchdir(prev)
        os.close(prev)




    try:
        os.chmod(sock_name, 0o700, dir_fd=dir_fd)
    except OSError:
        pass


def serve(name: str, spec: dict) -> int:

















    cwd = os.path.abspath(os.getcwd())
    sock_name, pid_name = socket_pid_names(cwd, name)
    dir_fd, base = open_runtime_dir()
    try:
        require_relative_ops(base)
        sock_path = os.path.join(base, sock_name)
        pid_path = os.path.join(base, pid_name)








        if not claim_pidfile(pid_name, dir_fd=dir_fd):
            sys.stderr.write(
                f"daemon: {name} already running (pidfile {pid_path}) — "
                "not starting a second\n")
            return 0


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












    try:
        os.unlink(sock_name, dir_fd=dir_fd)
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    _bind_in(server, sock_name, dir_fd)
    server.listen(8)
    server.settimeout(ACCEPT_POLL_SEC)


    cmd = spec.get("cmd")
    if not cmd:
        sys.exit(f"daemon: mcp.{name}.cmd missing")
    args = spec.get("args") or []
    if isinstance(cmd, str) and not args:
        argv = shlex.split(cmd)
    else:
        argv = [cmd] + list(args) if isinstance(cmd, str) else list(cmd) + list(args)




    proc = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, **(spec.get("env") or {})},
    )











    stderr_log = dbg_log = None
    try:



        def _safe_open(name: str, *, mode: int = 0o600) -> int:
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
            return os.open(name, flags, mode, dir_fd=dir_fd)
        stderr_log = os.fdopen(
            _safe_open(f"{sock_name}.stderr"), "ab", buffering=0)
        def drain_stderr() -> None:

            while True:
                line = proc.stderr.readline()
                if not line: break
                stderr_log.write(line)
        threading.Thread(target=drain_stderr, daemon=True).start()


        dbg_log = os.fdopen(_safe_open(f"{sock_name}.log"), "ab", buffering=0)
        def dbg(msg: str) -> None:
            dbg_log.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n".encode())





        shutting_down = [False]
        def shutdown(*_):
            shutting_down[0] = True
        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)

        last_activity = [time.time()]
        idle_timeout = int(spec.get("idle_timeout", IDLE_TIMEOUT_SEC))

        while not shutting_down[0]:

            if time.time() - last_activity[0] > idle_timeout:
                break

            if proc.poll() is not None:
                break
            try:
                client_sock, _ = server.accept()
            except socket.timeout:
                continue


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



    if do_detach:
        detach()

    return serve(name, spec)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
