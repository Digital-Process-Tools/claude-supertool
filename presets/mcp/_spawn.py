"""One warm daemon per (kind, config fingerprint) — the shared spawn path. #451

Four `phpstan-warm` daemons were found alive at once, the oldest thirteen hours
old, and the spread of uptimes said generational accumulation rather than a
single unlucky race. Two things allowed it, and each validator adapter carried
its own copy of both:

**The startup window.** "Is one already running?" was answered by
`os.path.exists(sock) and is_alive(pid)`. Both facts are published *late* — the
daemon has to launch an interpreter, detach through two forks, bind the socket
and write the pidfile before either is true. Every caller that looked inside
that window saw nothing and started its own daemon. Nothing serialised the
lookup with the spawn, so the check was advisory at best.

**No config identity.** A warm daemon is valuable precisely because it holds
loaded config and analysis cache across calls — which is the same thing as
saying an old daemon holds *old* config. Once `phpstan.neon` or `rector.php`
changes, the elder answers from a configuration that no longer exists on disk,
silently, in a shape indistinguishable from a correct answer.

The contract implemented here:

- Discovery is `sock exists` + `pid alive` (via the shared `_proc` probe, #429)
  + `fingerprint on disk == fingerprint of current config`. All three, or the
  daemon is not usable.
- Spawning happens under an exclusive `flock` on `<base>.lock`, with the
  discovery re-run *after* the lock is acquired. The loser of a race waits for
  the winner's daemon instead of starting a second one.
- A live daemon whose fingerprint no longer matches is **reaped, then
  respawned** — not left to answer. Refusing and letting the caller decide
  would leave the elder alive, which is the accumulation this closes.
- `_proc.pid_alive()` resolves an unanswerable question to *not alive*, which
  is the safe direction for a killer and the wrong one for a spawner. Here it
  only ever runs under the lock, and the daemon's own `O_EXCL` pidfile claim
  (daemon.py) is the authoritative gate — so "I can't tell" costs at most one
  extra process launch that immediately exits, never a surviving duplicate.

Config identity is the **content** of the config, not its mtime: the resolved
mcp spec (json, key-sorted) plus a sha256 of every existing file named in its
cmd/args/env. Re-saving a file without changing it keeps the daemon warm;
changing a byte retires it. The server binary itself is one of those files, so
upgrading `mcp-phpstan-warm` also retires the daemon running the old one.

Limit worth knowing: only files named *directly* in the spec are hashed. A
`phpstan.neon` that `includes:` another file changes fingerprint when the
top-level file changes, not when the include does.
"""
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
import _proc  # noqa: E402  (the one liveness probe — never os.kill(pid, 0), #429)
from _paths import socket_pid_paths  # noqa: E402

try:
    import fcntl
except ImportError:  # pragma: no cover — Windows
    fcntl = None  # type: ignore[assignment]

LOCK_WAIT_SEC = 60.0
LOCK_POLL_SEC = 0.05
REAP_GRACE_SEC = 3.0
SPAWN_TIMEOUT_SEC = 60.0


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

def _base(sock_path: str) -> str:
    return sock_path[:-5] if sock_path.endswith(".sock") else sock_path


def lock_path(sock_path: str) -> str:
    """Spawn mutex for one (cwd, name). Siblings of the socket, same 0700 dir."""
    return _base(sock_path) + ".lock"


def fingerprint_path(sock_path: str) -> str:
    """Where the running daemon records the config it booted with."""
    return _base(sock_path) + ".fp"


# --------------------------------------------------------------------------
# Config identity
# --------------------------------------------------------------------------

def load_spec(name: str, cwd: str) -> Optional[dict]:
    """Walk up from `cwd` for .supertool.json; return mcp[name], or None.

    Unlike daemon.py's loader this never exits — a caller that cannot find the
    spec still needs to produce *some* fingerprint rather than die.
    """
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
    """Existing files named in the spec — `--config=x.neon`, the binary, etc."""
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
    """Identity of the configuration a daemon would boot with.

    Content-addressed, so `touch` is not a change and a re-save with identical
    bytes is not a change. A missing spec fingerprints as `no-spec`, which
    matches nothing a real daemon ever wrote.
    """
    if not spec:
        return "no-spec"
    h = hashlib.sha256()
    h.update(json.dumps(spec, sort_keys=True, default=str).encode())
    for path in config_files(spec, cwd):
        h.update(b"\0")
        h.update(path.encode())
        h.update(_digest(path).encode())
    return h.hexdigest()[:16]


def write_fingerprint(sock_path: str, fingerprint: str) -> None:
    """Record the booted config. O_NOFOLLOW like every other file we create (#148)."""
    path = fingerprint_path(sock_path)
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(fingerprint)
    os.replace(tmp, path)  # atomic — a reader never sees a half-written fingerprint


def read_fingerprint(sock_path: str) -> str:
    try:
        return Path(fingerprint_path(sock_path)).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


# --------------------------------------------------------------------------
# Liveness
# --------------------------------------------------------------------------

def read_pid(pid_path: str) -> int:
    try:
        return int(Path(pid_path).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def daemon_pid(pid_path: str) -> int:
    """The pid of the live daemon named by this pidfile, or 0."""
    pid = read_pid(pid_path)
    return pid if pid > 0 and _proc.pid_alive(pid) else 0


def usable(sock_path: str, pid_path: str, fingerprint: str) -> bool:
    """Can a request be sent here right now, and answered with current config?

    All three clauses matter. Socket without pid: a stale file with no
    listener. Pid without socket: a daemon that died mid-publish, or one still
    booting. Either without a matching fingerprint: an answer computed from a
    config that is no longer on disk.
    """
    if not os.path.exists(sock_path):
        return False
    if not daemon_pid(pid_path):
        return False
    return read_fingerprint(sock_path) == fingerprint


def cleanup(sock_path: str, pid_path: str) -> None:
    for path in (sock_path, pid_path, fingerprint_path(sock_path)):
        try:
            os.unlink(path)
        except OSError:
            pass


def reap(pid: int, sock_path: str, pid_path: str, grace: float = REAP_GRACE_SEC) -> bool:
    """Retire a daemon and erase its footprint. SIGTERM, then SIGKILL.

    SIGTERM lets the daemon tear down its MCP server child; without that the
    heavy PHP process outlives its parent and becomes one of the strays this
    issue is about.
    """
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


# --------------------------------------------------------------------------
# Spawn mutex
# --------------------------------------------------------------------------

@contextlib.contextmanager
def spawn_lock(path: str, timeout: float = LOCK_WAIT_SEC):
    """Exclusive, cross-process lock held for the whole check-and-spawn.

    `flock` and not `lockf`: POSIX record locks are per-process, so two threads
    of one adapter process would both "acquire" them and race anyway. flock is
    per open file description — a second `open()` blocks even from the same
    process, which is exactly the case a single supertool run produces.

    Without `fcntl` (Windows) this degrades to no locking. The daemon's own
    `O_EXCL` pidfile claim still guarantees the invariant there; the lock is
    what keeps the *losing* caller from launching a process at all.
    """
    if fcntl is None:  # pragma: no cover — Windows
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
                    )
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


# --------------------------------------------------------------------------
# The one spawn path
# --------------------------------------------------------------------------

def ensure_daemon(
    cwd: str,
    name: str,
    *,
    preflight: Optional[Callable[[], None]] = None,
    spawn_timeout: float = SPAWN_TIMEOUT_SEC,
    lock_timeout: float = LOCK_WAIT_SEC,
    python: str = sys.executable,
) -> str:
    """Return the socket of *the* warm daemon for `name`, starting it if needed.

    `preflight` runs only on the spawn path — adapters use it to resolve their
    MCP server binary and raise a good error before a daemon is launched.

    `python` defaults to `sys.executable`, never the name `"python3"` (#564).
    All four adapters pass no `python=` at all, so the default *was* the
    interpreter every warm daemon ran under: a PATH lookup that resolves to
    the App Execution Alias stub on Windows — which blocks rather than errors
    (#529) — and, on POSIX, to whatever interpreter PATH names, which is not
    necessarily the virtualenv the adapter itself is running in. A daemon
    holding loaded config under a different set of installed packages than
    its caller is the same class of silent wrong answer as the stale
    fingerprint this module exists to prevent.
    """
    cwd = os.path.abspath(cwd)
    sock_path, pid_path = socket_pid_paths(cwd, name)
    fingerprint = config_fingerprint(load_spec(name, cwd), cwd)

    # Fast path: an in-date daemon is already answering. No lock, no syscall
    # storm — this is every call after the first.
    if usable(sock_path, pid_path, fingerprint):
        return sock_path

    with spawn_lock(lock_path(sock_path), timeout=lock_timeout):
        # Re-check under the lock. This is the whole fix for the startup
        # window: the caller that waited here finds the daemon the winner
        # started, instead of starting a second one.
        if usable(sock_path, pid_path, fingerprint):
            return sock_path

        existing = daemon_pid(pid_path)
        if existing:
            # Alive but unusable — stale config, or a socket that went away.
            # Reap rather than refuse: leaving it running is the accumulation.
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
