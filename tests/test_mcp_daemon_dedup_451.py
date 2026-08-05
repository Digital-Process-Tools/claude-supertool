"""Regression tests for #451: warm MCP daemons accumulate instead of being reused.

Observed: four `phpstan-warm` daemons alive at once, oldest 13h. The spread of
uptimes is generational accumulation — each spawn failed to find, or failed to
wait for, the daemon already starting.

Two defects are pinned here:

1. **The startup window.** `ensure_daemon()` decides "is one already running?"
   from `os.path.exists(sock) and is_alive(pid)`. Both are published *late* —
   the daemon binds the socket and writes the pidfile after a process launch,
   an interpreter start and a detach. Every caller that looks during that
   window sees nothing and spawns its own. The interesting case is therefore
   two spawns *racing during startup*, not two sequential calls.

2. **Config identity.** A warm daemon's whole value is that it holds config and
   analysis cache across calls — which means an elder daemon holds the config
   as it was when it booted. Nothing compared the running daemon's config to
   the current one, so a request could be served from a `phpstan.neon` that no
   longer exists on disk, silently, looking exactly like a correct answer.

Invariant: **at most one live warm daemon per (kind, config fingerprint), and a
request is never served by a daemon whose config has since changed.**

No real PHP daemons are spawned here — the process/pidfile layer is fixtured.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket as _socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not hasattr(_socket, "AF_UNIX"),
    reason="MCP daemon paths require AF_UNIX — not available on Windows runners.",
)

ROOT = Path(__file__).parent.parent
MCP_DIR = ROOT / "presets" / "mcp"
ADAPTER = ROOT / "validators" / "phpstan-mcp" / "phpstan-mcp.py"

sys.path.insert(0, str(MCP_DIR))
sys.path.insert(0, str(ROOT / "presets"))

if hasattr(_socket, "AF_UNIX"):
    import _proc  # noqa: E402
else:  # pragma: no cover
    _proc = None  # type: ignore[assignment]

# The real Popen, captured before any test monkeypatches it away.
_REAL_POPEN = subprocess.Popen

DAEMON_NAME = "phpstan-warm-451"


def _load(mod_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _spawn_module():
    """`presets/mcp/_spawn.py` — the shared spawn/dedup helper (may not exist yet)."""
    try:
        import _spawn  # noqa: PLC0415
        return _spawn
    except ImportError:  # pre-fix: the module this issue introduces
        return None


# --------------------------------------------------------------------------
# Fixtures — a project with a real .supertool.json and a real config file, a
# private runtime dir, and a fake spawner standing in for `daemon.py --detach`.
# --------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path):
    """A project dir whose mcp spec references an on-disk config file."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "phpstan.neon").write_text("parameters:\n  level: 8\n", encoding="utf-8")
    binary = proj / "mcp-phpstan-warm"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    (proj / ".supertool.json").write_text(json.dumps({
        "mcp": {
            DAEMON_NAME: {
                "cmd": [str(binary),
                        f"--working-dir={proj}",
                        f"--config={proj / 'phpstan.neon'}"],
                "idle_timeout": 1800,
            }
        }
    }), encoding="utf-8")
    return proj


@pytest.fixture
def runtime(monkeypatch):
    """Short runtime dir — AF_UNIX paths cap at ~104 bytes, pytest's tmp_path
    on macOS (`/private/var/folders/...`) blows straight past it."""
    d = tempfile.mkdtemp(prefix="st451-", dir=tempfile.gettempdir())
    monkeypatch.setenv("SUPERTOOL_RUNTIME_DIR", d)
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def adapter(runtime, monkeypatch, project):
    monkeypatch.setenv("MCP_PHPSTAN_DAEMON_NAME", DAEMON_NAME)
    monkeypatch.setenv("MCP_PHPSTAN_BIN", str(project / "mcp-phpstan-warm"))
    monkeypatch.setenv("MCP_PHPSTAN_WORKING_DIR", str(project))
    mod = _load("phpstan_mcp_451", ADAPTER)
    mod.SPAWN_TIMEOUT_SEC = 10  # keep the failure path short in tests
    return mod


@pytest.fixture
def sockets():
    """Track listening UDS objects so the fake daemons get torn down."""
    live = []
    yield live
    for s in live:
        try:
            s.close()
        except OSError:
            pass


@pytest.fixture
def victim():
    """A real, killable process standing in for a running daemon.

    Deliberately *not* a child of the test process: a child that has been
    SIGTERMed stays a zombie until someone waits on it, and a zombie answers
    `os.kill(pid, 0)` — it would read as alive and mask a working reap. A real
    daemon is detached (double-fork), so the fixture double-forks too.
    """
    pids = []

    def make() -> int:
        p = _REAL_POPEN(
            [sys.executable, "-c",
             "import os, sys, time\n"
             "if os.fork():\n"
             "    os._exit(0)\n"
             "print(os.getpid(), flush=True)\n"
             "time.sleep(120)\n"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            encoding="utf-8", errors="replace")
        pid = int(p.stdout.readline().strip())
        p.wait(timeout=10)
        pids.append(pid)
        return pid

    yield make
    for pid in pids:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def _bind(sock_path: str, sockets: list) -> None:
    """Create a *real* listening socket at `sock_path`, as the daemon would."""
    try:
        os.unlink(sock_path)
    except FileNotFoundError:
        pass
    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    s.bind(sock_path)
    s.listen(8)
    sockets.append(s)


def _publish(sock_path: str, pid_path: str, pid: int, cwd: str, sockets: list) -> None:
    """Do what a started daemon does: bind the socket, write pid + fingerprint.

    The fingerprint write is best-effort so this helper works against both the
    pre-fix code (no fingerprint concept — nothing reads the file) and the
    fixed code (fingerprint is how config identity is established).
    """
    _bind(sock_path, sockets)
    Path(pid_path).write_text(str(pid), encoding="utf-8")
    sp = _spawn_module()
    if sp is not None:
        spec = sp.load_spec(DAEMON_NAME, cwd) or {}
        sp.write_fingerprint(sock_path, sp.config_fingerprint(spec, cwd))


class FakeSpawn:
    """Stands in for `python3 daemon.py NAME --detach`.

    Crucially it publishes the socket/pidfile *late* — `Popen` returns
    immediately, and the daemon only becomes discoverable `delay` seconds
    later. That delay is the startup window in which duplicates are born.
    """

    def __init__(self, sock_path, pid_path, cwd, sockets, pid, delay=0.4):
        self.sock_path = sock_path
        self.pid_path = pid_path
        self.cwd = cwd
        self.sockets = sockets
        self.pid = pid
        self.delay = delay
        self.calls = []
        self._lock = threading.Lock()
        self._threads = []

    def __call__(self, argv, *a, **kw):
        with self._lock:
            self.calls.append(list(argv))

        def publish():
            time.sleep(self.delay)
            _publish(self.sock_path, self.pid_path, self.pid, self.cwd, self.sockets)

        t = threading.Thread(target=publish, daemon=True)
        t.start()
        self._threads.append(t)
        return _FakeProc()

    @property
    def count(self) -> int:
        with self._lock:
            return len(self.calls)

    def join(self) -> None:
        for t in self._threads:
            t.join(timeout=5)


class _FakeProc:
    pid = -1

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0


# --------------------------------------------------------------------------
# 1. The startup-window race — the case that actually produces four daemons.
# --------------------------------------------------------------------------


class TestStartupWindowRace:
    def test_concurrent_callers_spawn_one_daemon(
        self, adapter, project, runtime, sockets, victim, monkeypatch
    ):
        """Two callers arriving inside the startup window must spawn once, not twice.

        Both look while the socket and pidfile are still unpublished. Pre-fix
        both see "nothing running" and launch their own daemon — generational
        accumulation, one generation at a time.
        """
        sock, pid_path = adapter.sock_paths(str(project), DAEMON_NAME)
        fake = FakeSpawn(sock, pid_path, str(project), sockets, victim(), delay=0.4)
        monkeypatch.setattr(subprocess, "Popen", fake)

        results: list = []
        errors: list = []

        def call():
            try:
                results.append(adapter.ensure_daemon(str(project)))
            except Exception as e:  # noqa: BLE001 — surfaced in the assertion below
                errors.append(e)

        threads = [threading.Thread(target=call) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        fake.join()

        assert not errors, f"ensure_daemon raised: {errors}"
        assert fake.count == 1, (
            f"#451: {fake.count} daemons spawned for one (kind, config) — "
            "concurrent callers inside the startup window each spawned their own"
        )
        assert results == [sock, sock]

    def test_second_caller_waits_for_the_first_daemon(
        self, adapter, project, runtime, sockets, victim, monkeypatch
    ):
        """The caller that loses the race still gets a usable, connectable socket.

        Deduplication that hands back a path nothing is listening on would trade
        four daemons for zero — worse.
        """
        sock, pid_path = adapter.sock_paths(str(project), DAEMON_NAME)
        fake = FakeSpawn(sock, pid_path, str(project), sockets, victim(), delay=0.4)
        monkeypatch.setattr(subprocess, "Popen", fake)

        out: list = []
        threads = [threading.Thread(target=lambda: out.append(adapter.ensure_daemon(str(project))))
                   for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        fake.join()

        assert fake.count == 1, f"#451: {fake.count} spawns from 3 concurrent callers"
        assert len(out) == 3
        c = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        try:
            c.settimeout(5)
            c.connect(out[0])  # raises if the returned socket has no listener
        finally:
            c.close()


# --------------------------------------------------------------------------
# 2. Config identity — an elder daemon must not answer with yesterday's config.
# --------------------------------------------------------------------------


class TestConfigFingerprint:
    def test_changed_config_reaps_and_respawns(
        self, adapter, project, runtime, sockets, victim, monkeypatch
    ):
        """Editing phpstan.neon must retire the daemon that booted on the old one."""
        sock, pid_path = adapter.sock_paths(str(project), DAEMON_NAME)
        old_pid = victim()
        _publish(sock, pid_path, old_pid, str(project), sockets)

        (project / "phpstan.neon").write_text("parameters:\n  level: 9\n", encoding="utf-8")

        fake = FakeSpawn(sock, pid_path, str(project), sockets, victim(), delay=0.1)
        monkeypatch.setattr(subprocess, "Popen", fake)
        got = adapter.ensure_daemon(str(project))
        fake.join()

        assert got == sock
        assert fake.count == 1, (
            "#451: config changed under a warm daemon and no fresh daemon was "
            "started — the answer would come from a config that no longer exists"
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and _proc.pid_alive(old_pid):
            time.sleep(0.1)
        assert not _proc.pid_alive(old_pid), \
            "#451: stale-config daemon left running — it accumulates"

    def test_unchanged_config_reuses_the_running_daemon(
        self, adapter, project, runtime, sockets, victim, monkeypatch
    ):
        """The other direction: an in-date daemon is reused, never respawned.

        Guards against 'fix' the accumulation by churning a daemon per call.
        """
        sock, pid_path = adapter.sock_paths(str(project), DAEMON_NAME)
        pid = victim()
        _publish(sock, pid_path, pid, str(project), sockets)

        fake = FakeSpawn(sock, pid_path, str(project), sockets, -1, delay=0.1)
        monkeypatch.setattr(subprocess, "Popen", fake)
        got = adapter.ensure_daemon(str(project))

        assert got == sock
        assert fake.count == 0, "warm daemon with current config must be reused as-is"
        assert _proc.pid_alive(pid), "a current daemon must not be reaped"

    def test_fingerprint_tracks_referenced_config_content(self, project, runtime):
        """Config identity is content of the referenced files, not their mtime."""
        sp = _spawn_module()
        assert sp is not None, "#451: presets/mcp/_spawn.py not present"
        spec = sp.load_spec(DAEMON_NAME, str(project))
        first = sp.config_fingerprint(spec, str(project))

        os.utime(project / "phpstan.neon", (time.time() + 5000, time.time() + 5000))
        assert sp.config_fingerprint(spec, str(project)) == first, \
            "touching a config file must not invalidate a warm daemon"

        (project / "phpstan.neon").write_text("parameters:\n  level: 9\n", encoding="utf-8")
        assert sp.config_fingerprint(spec, str(project)) != first, \
            "editing a referenced config file must invalidate the daemon"

        (project / "unrelated.txt").write_text("noise", encoding="utf-8")
        assert sp.config_fingerprint(spec, str(project)) == \
            sp.config_fingerprint(spec, str(project)), "fingerprint must be deterministic"

    def test_fingerprint_tracks_the_spec_itself(self, project, runtime):
        sp = _spawn_module()
        assert sp is not None, "#451: presets/mcp/_spawn.py not present"
        spec = sp.load_spec(DAEMON_NAME, str(project))
        before = sp.config_fingerprint(spec, str(project))
        changed = dict(spec)
        changed["cmd"] = list(spec["cmd"]) + ["--level=9"]
        assert sp.config_fingerprint(changed, str(project)) != before, \
            "a changed daemon command line is a different daemon"


# --------------------------------------------------------------------------
# 3. The opposite failure: no daemon at all.
# --------------------------------------------------------------------------


class TestStaleStateDoesNotBlockSpawn:
    def test_dead_pid_still_spawns(
        self, adapter, project, runtime, sockets, victim, monkeypatch
    ):
        """A crashed daemon's leftover pidfile must not wedge the spawner."""
        sock, pid_path = adapter.sock_paths(str(project), DAEMON_NAME)
        dead = _REAL_POPEN([sys.executable, "-c", "pass"])
        dead.wait(timeout=10)
        if _proc.pid_alive(dead.pid):  # pragma: no cover — pid reuse
            pytest.skip("pid reused before the test could use it")
        _bind(sock, sockets)
        Path(pid_path).write_text(str(dead.pid), encoding="utf-8")

        fake = FakeSpawn(sock, pid_path, str(project), sockets, victim(), delay=0.1)
        monkeypatch.setattr(subprocess, "Popen", fake)
        got = adapter.ensure_daemon(str(project))
        fake.join()

        assert got == sock
        assert fake.count == 1, "a dead daemon must be replaced, not mourned"

    def test_missing_socket_with_live_pid_respawns(
        self, adapter, project, runtime, sockets, victim, monkeypatch
    ):
        """Pidfile alive but socket gone — half-published state, still no listener."""
        sock, pid_path = adapter.sock_paths(str(project), DAEMON_NAME)
        Path(pid_path).parent.mkdir(parents=True, exist_ok=True)
        Path(pid_path).write_text(str(victim()), encoding="utf-8")

        fake = FakeSpawn(sock, pid_path, str(project), sockets, victim(), delay=0.1)
        monkeypatch.setattr(subprocess, "Popen", fake)
        got = adapter.ensure_daemon(str(project))
        fake.join()

        assert got == sock
        assert fake.count == 1


# --------------------------------------------------------------------------
# 4. daemon.py's own guard must come before its side effects.
# --------------------------------------------------------------------------


class TestDaemonClaimsBeforeSideEffects:
    def test_serve_does_not_bind_or_spawn_when_already_running(
        self, project, runtime, sockets, monkeypatch
    ):
        """`serve()` must claim the pidfile *first*.

        Pre-fix it unlinks the socket, binds its own, and launches the MCP
        server subprocess, and only then discovers the pidfile is taken and
        exits — leaving the incumbent daemon's socket path stolen and a heavy
        PHP child orphaned. That is the engine of the accumulation.
        """
        import daemon as mcp_daemon  # noqa: PLC0415

        monkeypatch.chdir(project)
        sock, pid_path = mcp_daemon.socket_pid_paths(str(project), DAEMON_NAME)
        _bind(sock, sockets)
        inode_before = os.stat(sock).st_ino
        # The incumbent: us. Alive by construction.
        Path(pid_path).write_text(str(os.getpid()), encoding="utf-8")

        spawned: list = []
        monkeypatch.setattr(subprocess, "Popen",
                            lambda argv, *a, **kw: spawned.append(list(argv)))

        spec = json.loads((project / ".supertool.json").read_text(encoding="utf-8"))
        try:
            mcp_daemon.serve(DAEMON_NAME, spec["mcp"][DAEMON_NAME])
        except SystemExit:
            pass

        assert spawned == [], \
            "#451: serve() spawned the MCP server before checking the pidfile — orphan"
        assert os.path.exists(sock), "#451: serve() unlinked the incumbent's socket"
        assert os.stat(sock).st_ino == inode_before, \
            "#451: serve() rebound the socket path, making the incumbent unreachable"
        assert Path(pid_path).read_text(encoding="utf-8").strip() == str(os.getpid()), \
            "the incumbent's pidfile must be left alone"
