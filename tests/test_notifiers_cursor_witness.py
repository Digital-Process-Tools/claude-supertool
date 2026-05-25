"""Tests for the cursor-witness notifier — emits supertool events to a UDS listener.

The notifier writes a JSON line per event; the listener (stand-in for the VSCode
extension) reads them. These tests pin the wire format and the silent-on-no-listener
behavior so the parent supertool call never breaks.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"),
    reason="cursor-witness notifier uses AF_UNIX sockets — not available on this platform",
)


NOTIFY_SCRIPT = str(Path(__file__).parent.parent / "notifiers" / "cursor-witness" / "notify.py")


def _spawn_listener(sock_path: str) -> tuple[socket.socket, list]:
    """Bind a real UDS server inline (no subprocess) and return (sock, captured_lines).

    Inline is easier to assert on than spawning listen.py — same wire format.
    """
    try: os.unlink(sock_path)
    except FileNotFoundError: pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    srv.listen(8)
    srv.settimeout(2.0)
    return srv, []


def _accept_one(srv: socket.socket) -> dict | None:
    """Accept one connection, return its parsed JSON payload or None on timeout."""
    try:
        client, _ = srv.accept()
    except socket.timeout:
        return None
    try:
        buf = b""
        while b"\n" not in buf and len(buf) < 8192:
            chunk = client.recv(4096)
            if not chunk:
                break
            buf += chunk
        line = buf.split(b"\n", 1)[0]
        if not line:
            return None
        return json.loads(line.decode("utf-8"))
    finally:
        client.close()


def test_notify_writes_to_listening_socket(tmp_path: Path) -> None:
    sock_path = f"/tmp/st-witness-test-{uuid.uuid4().hex[:8]}.sock"
    srv, _ = _spawn_listener(sock_path)
    env = os.environ.copy()
    env["SUPERTOOL_WITNESS_SOCKET"] = sock_path
    try:
        subprocess.run(
            [sys.executable, NOTIFY_SCRIPT, "edit", "/abs/path/foo.php", "42"],
            env=env, timeout=3,
        )
        event = _accept_one(srv)
        assert event is not None
        assert event["op"] == "edit"
        assert event["file"] == "/abs/path/foo.php"
        assert event["line"] == 42
        assert "ts" in event
        assert "cwd" in event
    finally:
        srv.close()
        try: os.unlink(sock_path)
        except FileNotFoundError: pass


def test_notify_silent_when_no_listener(tmp_path: Path) -> None:
    """Without a listener the notifier must exit cleanly (don't break parent)."""
    sock_path = f"/tmp/st-witness-nolisten-{uuid.uuid4().hex[:8]}.sock"
    env = os.environ.copy()
    env["SUPERTOOL_WITNESS_SOCKET"] = sock_path
    r = subprocess.run(
        [sys.executable, NOTIFY_SCRIPT, "edit", "/abs/path/foo.php", "1"],
        env=env, capture_output=True, timeout=3,
    )
    assert r.returncode == 0
    assert r.stdout == b""
    assert r.stderr == b""


def test_notify_line_optional(tmp_path: Path) -> None:
    """Missing or non-numeric line arg → line=None in payload."""
    sock_path = f"/tmp/st-witness-noline-{uuid.uuid4().hex[:8]}.sock"
    srv, _ = _spawn_listener(sock_path)
    env = os.environ.copy()
    env["SUPERTOOL_WITNESS_SOCKET"] = sock_path
    try:
        subprocess.run(
            [sys.executable, NOTIFY_SCRIPT, "read", "/abs/path/foo.php"],
            env=env, timeout=3,
        )
        event = _accept_one(srv)
        assert event is not None
        assert event["op"] == "read"
        assert event["line"] is None
    finally:
        srv.close()
        try: os.unlink(sock_path)
        except FileNotFoundError: pass


def test_notify_handles_missing_args(tmp_path: Path) -> None:
    """Bad invocation (< 3 args) exits clean, no socket activity."""
    r = subprocess.run(
        [sys.executable, NOTIFY_SCRIPT, "edit"],
        capture_output=True, timeout=3,
    )
    assert r.returncode == 0
