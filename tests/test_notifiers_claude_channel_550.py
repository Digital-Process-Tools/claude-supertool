"""End-to-end tests for `claude-channel` socket ownership (issue #550).

`channel.ts` used to `unlinkSync(SOCK_PATH)` unconditionally before binding.
A UDS path is a *name*; a connection is to the *inode*. So a second server
unlinked the path, the incumbent's inode lost its name but kept `listen()`ing,
the newcomer bound a fresh inode at the same path with no `EADDRINUSE` (it had
just freed the name itself), and every poller from then on reached the
newcomer. The incumbent got no error, no close and no log — a dead radar that
looked exactly like an all-green one, in the subsystem whose job is noticing
when things break.

The contract these tests pin:

    a claude-channel server either owns the watch socket or exits non-zero
    saying why — no participant is ever silently blinded.

and, because refusing to start is only safe if the population of live
incumbents is honest, the second half of the same contract:

    a server whose stdio client is gone releases the socket and exits.

Issue #550's own evidence table had two orphaned servers whose parent sessions
were long dead. Without that second rule, "refuse when someone is listening"
would mean one leaked process permanently denies every future session a radar
— trading a silent bug for a loud one, which is not an improvement.

Coverage, same as `test_notifiers_claude_channel_554.py`: the `notifiers` job in
`.github/workflows/tests.yml` installs bun and runs this file for real on ubuntu
and macOS, under `SUPERTOOL_REQUIRE_JS=1` so a missing prerequisite is a
collection error rather than a silent skip (#557). The twelve-leg pytest matrix
installs no JS runtime and still skips it, with the reason printed.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import socket as _socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest

from _toolchain_gate import js_promised, require_or_skip

REPO = Path(__file__).resolve().parents[1]
CHANNEL_TS = REPO / "notifiers" / "claude-channel" / "channel.ts"
NODE_MODULES = REPO / "notifiers" / "claude-channel" / "node_modules"

pytestmark = [
    require_or_skip(
        hasattr(_socket, "AF_UNIX"),
        "claude-channel binds an AF_UNIX socket — not available on this platform",
        promised=js_promised(),
    ),
    require_or_skip(
        shutil.which("bun") is not None,
        "claude-channel runs under bun; no bun on PATH",
        promised=js_promised(),
    ),
    require_or_skip(
        NODE_MODULES.exists(),
        "channel deps not installed — run notifiers/claude-channel/install.sh",
        promised=js_promised(),
    ),
]

CHANNEL_METHOD = "notifications/claude/channel"
#: `channel.ts` exits with this when it will not take a socket someone else owns.
EXIT_SOCKET_CONFLICT = 3


class Channel:
    """A live `channel.ts` process plus a minimal MCP client over its stdio.

    Unlike the #554 harness this one takes an explicit socket path (two servers
    must be pointed at the *same* path to reproduce the theft) and can be told
    not to expect a successful start, since half these tests are about a server
    that is supposed to refuse.
    """

    def __init__(self, sock_path: str, *, expect_start: bool = True) -> None:
        self.sock_path = sock_path
        self.proc = subprocess.Popen(
            ["bun", str(CHANNEL_TS)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(CHANNEL_TS.parent),
            env={**os.environ, "SUPERTOOL_WATCH_SOCK": sock_path},
            text=True,
            bufsize=1,
        )
        self.messages: "queue.Queue[dict]" = queue.Queue()
        self.stderr_lines: list[str] = []
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()
        if expect_start:
            self._handshake()
            self._await_socket()

    def _pump_stdout(self) -> None:
        for line in self.proc.stdout:  # type: ignore[union-attr]
            line = line.strip()
            if not line:
                continue
            try:
                self.messages.put(json.loads(line))
            except json.JSONDecodeError:
                pass

    def _pump_stderr(self) -> None:
        for line in self.proc.stderr:  # type: ignore[union-attr]
            self.stderr_lines.append(line.rstrip())

    def _send(self, msg: dict) -> None:
        self.proc.stdin.write(json.dumps(msg) + "\n")  # type: ignore[union-attr]
        self.proc.stdin.flush()  # type: ignore[union-attr]

    def _handshake(self) -> None:
        self._send({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"experimental": {"claude/channel": {}}},
                "clientInfo": {"name": "test-550", "version": "0"},
            },
        })
        result = self.next_message(timeout=15.0, method=None)
        assert "result" in result, f"initialize failed: {result}"
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _await_socket(self, timeout: float = 15.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(self.sock_path):
                return
            if self.proc.poll() is not None:
                raise AssertionError(
                    f"channel.ts exited {self.proc.returncode}: {self.stderr_lines}")
            time.sleep(0.02)
        raise AssertionError(f"socket never appeared at {self.sock_path}")

    def emit(self, event: dict) -> None:
        """Write one NDJSON line to the watch socket, exactly as a poller does."""
        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        s.connect(self.sock_path)
        s.sendall((json.dumps(event) + "\n").encode("utf-8"))
        s.close()

    def next_message(self, timeout: float = 5.0, method: str | None = CHANNEL_METHOD) -> dict:
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise AssertionError(
                    f"no message (method={method}) within {timeout}s; "
                    f"stderr={self.stderr_lines}")
            try:
                msg = self.messages.get(timeout=remaining)
            except queue.Empty:
                continue
            if method is None or msg.get("method") == method:
                return msg

    def wait_exit(self, timeout: float = 20.0) -> int:
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise AssertionError(
                f"channel.ts still running after {timeout}s; "
                f"stderr={self.stderr_lines}") from None

    def close(self) -> None:
        self.proc.kill()
        self.proc.wait(timeout=10)


@pytest.fixture()
def sock_dir():
    # macOS caps AF_UNIX paths at ~104 bytes; keep it under /tmp, not tmp_path.
    d = tempfile.mkdtemp(prefix="st550-", dir="/tmp")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture()
def sock_path(sock_dir):
    return os.path.join(sock_dir, "w.sock")


def _spawn(sock_path: str, *, expect_start: bool = True) -> Channel:
    return Channel(sock_path, expect_start=expect_start)


def test_a_second_server_does_not_steal_a_live_socket(sock_path: str) -> None:
    """The bug, end to end: A keeps its radar and B does not silently take it.

    Before the fix B unlinked A's path, bound its own inode there, and the
    event below arrived at B while A sat listening on an unnamed inode with
    nothing to report and no way to know.
    """
    a = _spawn(sock_path)
    b = None
    try:
        b = _spawn(sock_path, expect_start=False)
        code = b.wait_exit()
        assert code == EXIT_SOCKET_CONFLICT, (
            f"second server exited {code}, expected {EXIT_SOCKET_CONFLICT} "
            f"(socket conflict); stderr={b.stderr_lines}")

        # A must still own the path and still be delivering.
        a.emit({
            "ts": "2026-07-30T09:00:00Z", "source": "gitlab-mr", "id": "550",
            "event": "pipeline_failed", "payload": {"title": "incumbent still hears"},
        })
        msg = a.next_message()
        assert msg["params"]["meta"]["id"] == "550"
        assert a.proc.poll() is None, "incumbent died"
    finally:
        if b is not None:
            b.close()
        a.close()


def test_the_refusal_is_loud_and_names_the_way_out(sock_path: str) -> None:
    """A refusal nobody can act on is only a quieter kind of blindness.

    stderr must name the socket, say another server owns it, and point at the
    per-session override that lets this session have a radar of its own.
    """
    a = _spawn(sock_path)
    b = None
    try:
        b = _spawn(sock_path, expect_start=False)
        b.wait_exit()
        blob = "\n".join(b.stderr_lines)
        assert "claude-channel" in blob, f"unattributed error: {blob!r}"
        assert sock_path in blob, f"error does not name the socket: {blob!r}"
        assert "SUPERTOOL_WATCH_SOCK" in blob, (
            f"error does not point at the per-session override: {blob!r}")
        assert "550" in blob, f"error does not cite the issue: {blob!r}"
    finally:
        if b is not None:
            b.close()
        a.close()


def test_a_genuinely_stale_socket_file_is_still_reclaimed(sock_path: str) -> None:
    """The case the original `unlink` existed for must keep working.

    A crashed server leaves a socket file with no listener behind. Refusing to
    start on *that* would trade a silent bug for a server that never comes back
    after a crash.
    """
    dead = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    dead.bind(sock_path)
    dead.listen(1)
    dead.close()  # file survives; connect() to it now gives ECONNREFUSED
    assert os.path.exists(sock_path)

    a = _spawn(sock_path)
    try:
        a.emit({
            "ts": "2026-07-30T09:00:00Z", "source": "gitlab-mr", "id": "551",
            "event": "pipeline_succeeded", "payload": {"title": "after a crash"},
        })
        assert a.next_message()["params"]["meta"]["id"] == "551"
    finally:
        a.close()


def test_a_server_whose_client_is_gone_releases_the_socket(sock_path: str) -> None:
    """An MCP server outliving its session is what makes refusal dangerous.

    #550 observed two such orphans holding the path with their parent sessions
    long dead. When stdio closes there is nobody left to notify, so the server
    must drop the socket and exit rather than deny it to the next session.
    """
    a = _spawn(sock_path)
    try:
        a.proc.stdin.close()  # type: ignore[union-attr]
        code = a.wait_exit(timeout=15.0)
        assert code == 0, f"orphaned server exited {code}; stderr={a.stderr_lines}"
        assert not os.path.exists(sock_path), (
            "orphaned server left its socket file behind")
    finally:
        a.close()


def test_a_new_session_can_bind_after_the_previous_one_is_gone(sock_path: str) -> None:
    """The common case — restart your session — must not need manual cleanup."""
    a = _spawn(sock_path)
    a.proc.stdin.close()  # type: ignore[union-attr]
    a.wait_exit(timeout=15.0)
    a.close()

    b = _spawn(sock_path)
    try:
        b.emit({
            "ts": "2026-07-30T09:01:00Z", "source": "gitlab-mr", "id": "552",
            "event": "pipeline_failed", "payload": {"title": "restarted session"},
        })
        assert b.next_message()["params"]["meta"]["id"] == "552"
    finally:
        b.close()
