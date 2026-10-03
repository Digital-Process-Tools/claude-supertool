














































from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _proc  
from _paths import socket_pid_paths  

try:
    import fcntl
except ImportError:  
    fcntl = None  

LOCK_WAIT_SEC = 60.0
LOCK_POLL_SEC = 0.05
REAP_GRACE_SEC = 3.0
SPAWN_TIMEOUT_SEC = 60.0










AUTOSPAWN_ENV = "SUPERTOOL_MCP_AUTOSPAWN"




AUTOSPAWN_FALSEY = frozenset({"0", "false", "no", "off"})


class AutospawnSuppressed(RuntimeError):
    pass













def autospawn_allowed() -> bool:






    raw = os.environ.get(AUTOSPAWN_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in AUTOSPAWN_FALSEY






def _base(sock_path: str) -> str:
    return sock_path[:-5] if sock_path.endswith(".sock") else sock_path


def lock_path(sock_path: str) -> str:

    return _base(sock_path) + ".lock"


def pid_path(sock_path: str) -> str:








































    return _base(sock_path) + ".pid"


def fingerprint_path(sock_path: str) -> str:

    return _base(sock_path) + ".fp"






def load_spec(name: str, cwd: str) -> Optional[dict]:





    d = os.path.abspath(cwd)
    while True:
        p = os.path.join(d, ".supertool.json")
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    cfg = json.load(f)
            except (OSError, ValueError):
                return None
            return (cfg.get("mcp") or {}).get(name)
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _spec_tokens(spec: dict) -> list:
    cmd = spec.get("cmd")
    if isinstance(cmd, str):
        tokens = shlex.split(cmd)
    elif isinstance(cmd, (list, tuple)):
        tokens = [str(t) for t in cmd]
    else:
        tokens = []
    tokens += [str(a) for a in (spec.get("args") or [])]
    env = spec.get("env")
    if isinstance(env, dict):
        tokens += [str(v) for v in env.values()]
    return tokens


def config_files(spec: dict, cwd: str) -> list:

    found = set()
    for token in _spec_tokens(spec):
        candidate = token
        if candidate.startswith("-") and "=" in candidate:
            candidate = candidate.split("=", 1)[1]
        if not candidate:
            continue
        path = candidate if os.path.isabs(candidate) else os.path.join(cwd, candidate)
        if os.path.isfile(path):
            found.add(os.path.abspath(path))
    return sorted(found)


def _digest(path: str) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
    except OSError:
        return "unreadable"
    return h.hexdigest()


def config_fingerprint(spec: Optional[dict], cwd: str) -> str:






    if not spec:
        return "no-spec"
    h = hashlib.sha256()
    h.update(json.dumps(spec, sort_keys=True, default=str).encode())
    for path in config_files(spec, cwd):
        h.update(b"\0")
        h.update(path.encode())
        h.update(_digest(path).encode())
    return h.hexdigest()[:16]


def write_fingerprint(sock_path: str, fingerprint: str, *,
                      dir_fd: Optional[int] = None) -> None:







    path = fingerprint_path(sock_path)
    tmp = f"{path}.{os.getpid()}.tmp"
    kw = {} if dir_fd is None else {"dir_fd": dir_fd}
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600, **kw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(fingerprint)

        if dir_fd is None:
            os.replace(tmp, path)
        else:
            os.replace(tmp, path, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:



        try:
            os.unlink(tmp, **kw)
        except OSError:
            pass
        raise


def read_fingerprint(sock_path: str) -> str:
    try:
        return Path(fingerprint_path(sock_path)).read_text(encoding="utf-8").strip()
    except OSError:
        return ""






def read_pid(pid_path: str) -> int:
    try:
        return int(Path(pid_path).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def daemon_pid(pid_path: str) -> int:

    pid = read_pid(pid_path)
    return pid if pid > 0 and _proc.pid_alive(pid) else 0


def usable(sock_path: str, pid_path: str, fingerprint: str) -> bool:







    if not os.path.exists(sock_path):
        return False
    if not daemon_pid(pid_path):
        return False
    return read_fingerprint(sock_path) == fingerprint


def cleanup(sock_path: str, pid_path: str, *, dir_fd: Optional[int] = None) -> None:

    kw = {} if dir_fd is None else {"dir_fd": dir_fd}
    for path in (sock_path, pid_path, fingerprint_path(sock_path)):
        try:
            os.unlink(path, **kw)
        except OSError:
            pass


def reap(pid: int, sock_path: str, pid_path: str, grace: float = REAP_GRACE_SEC) -> bool:






    if pid > 0:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pid = 0
        except PermissionError:
            return False
    deadline = time.monotonic() + grace
    while pid and time.monotonic() < deadline:
        if not _proc.pid_alive(pid):
            pid = 0
            break
        time.sleep(0.05)
    if pid and _proc.pid_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and _proc.pid_alive(pid):
            time.sleep(0.05)
    cleanup(sock_path, pid_path)
    return not (pid and _proc.pid_alive(pid))






@contextlib.contextmanager
def spawn_lock(path: str, timeout: float = LOCK_WAIT_SEC):











    if fcntl is None:  
        yield False
        return
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"timed out after {timeout}s waiting for the daemon spawn lock "
                        f"({path}) — another caller is starting it"
                    ) from None
                time.sleep(LOCK_POLL_SEC)
        try:
            yield True
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        os.close(fd)


def force_respawn(
    cwd: str,
    name: str,
    *,
    preflight: Optional[Callable[[], None]] = None,
    spawn_timeout: float = SPAWN_TIMEOUT_SEC,
    lock_timeout: float = LOCK_WAIT_SEC,
    python: str = sys.executable,
) -> str:

















    cwd = os.path.abspath(cwd)
    sock_path, pid_path = socket_pid_paths(cwd, name)
    with spawn_lock(lock_path(sock_path), timeout=lock_timeout):
        existing = daemon_pid(pid_path)
        if existing:
            reap(existing, sock_path, pid_path)
        else:
            cleanup(sock_path, pid_path)
    return ensure_daemon(
        cwd, name, preflight=preflight, spawn_timeout=spawn_timeout,
        lock_timeout=lock_timeout, python=python)






def ensure_daemon(
    cwd: str,
    name: str,
    *,
    preflight: Optional[Callable[[], None]] = None,
    spawn_timeout: float = SPAWN_TIMEOUT_SEC,
    lock_timeout: float = LOCK_WAIT_SEC,
    python: str = sys.executable,
) -> str:















    cwd = os.path.abspath(cwd)
    sock_path, pid_path = socket_pid_paths(cwd, name)
    fingerprint = config_fingerprint(load_spec(name, cwd), cwd)



    if usable(sock_path, pid_path, fingerprint):
        return sock_path


























    if not autospawn_allowed():





        raise AutospawnSuppressed(
            f"no warm '{name}' daemon, and {AUTOSPAWN_ENV}="
            f"{os.environ.get(AUTOSPAWN_ENV)!r} forbids starting one, so none "
            f"was started. Warm it from a caller that can wait -- "
            f"supertool 'mcp_daemon:{name} --detach' -- or set "
            f"'mcp_autospawn': true on this validator if its timeout covers a "
            f"cold start (#475)."
        )

    with spawn_lock(lock_path(sock_path), timeout=lock_timeout):



        if usable(sock_path, pid_path, fingerprint):
            return sock_path

        existing = daemon_pid(pid_path)
        if existing:


            reap(existing, sock_path, pid_path)
        else:
            cleanup(sock_path, pid_path)

        if preflight is not None:
            preflight()

        daemon_script = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "daemon.py")
        if not os.path.isfile(daemon_script):
            raise RuntimeError(f"daemon.py not found: {daemon_script}")

        proc = subprocess.Popen(
            [python, daemon_script, name, "--detach"],
            cwd=cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

        deadline = time.monotonic() + spawn_timeout
        while time.monotonic() < deadline:
            if usable(sock_path, pid_path, fingerprint):
                return sock_path
            time.sleep(0.05)
        raise RuntimeError(
            f"daemon '{name}' did not publish a usable socket at {sock_path} "
            f"within {spawn_timeout}s"
        )
