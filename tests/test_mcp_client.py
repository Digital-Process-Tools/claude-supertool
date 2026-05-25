"""Tests for MCP client primitives in supertool.py.

The mock server (tests/fixtures/mock_mcp_server.py) listens on a per-test Unix socket
and speaks NDJSON JSON-RPC 2.0 — same wire format MCPClient uses against the daemon.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

import socket as _socket

_REQUIRES_AF_UNIX = pytest.mark.skipif(
    not hasattr(_socket, "AF_UNIX"),
    reason="MCP daemon uses AF_UNIX sockets — not available on this platform",
)

import supertool
from supertool import (
    MCPClient, MCPServerError, MCPTimeout,
    _mcp_call, _mcp_get_server, _mcp_register, _MCP_SERVERS, _MCP_LOCK,
)

MOCK_SERVER = str(Path(__file__).parent / "fixtures" / "mock_mcp_server.py")


@pytest.fixture
def mock_uds():
    """Spawn the UDS mock MCP server (sockets in /tmp/ — AF_UNIX path limit on macOS)."""
    if not hasattr(_socket, "AF_UNIX"):
        import pytest as _pytest
        _pytest.skip("MCP daemon uses AF_UNIX sockets — not available on this platform")
    sock_path = f"/tmp/st-mock-{uuid.uuid4().hex[:8]}.sock"
    proc = subprocess.Popen([sys.executable, MOCK_SERVER, sock_path])
    deadline = time.time() + 5
    while time.time() < deadline and not os.path.exists(sock_path):
        time.sleep(0.05)
    if not os.path.exists(sock_path):
        proc.terminate()
        raise RuntimeError(f"mock did not bind {sock_path}")
    try:
        yield sock_path
    finally:
        proc.terminate()
        try: proc.wait(timeout=3)
        except subprocess.TimeoutExpired: proc.kill()


def _make_client(sock_path: str, timeout: int = 5) -> MCPClient:
    return MCPClient(name="test", timeout=timeout, socket_path=sock_path)


# ---------------------------------------------------------------------------
# spawn (connect) + is_alive
# ---------------------------------------------------------------------------

def test_spawn_connects_to_daemon(mock_uds: str) -> None:
    c = _make_client(mock_uds)
    assert not c.is_alive()
    c.spawn()
    assert c.is_alive()
    c.shutdown()


def test_spawn_is_idempotent(mock_uds: str) -> None:
    c = _make_client(mock_uds)
    c.spawn()
    sock1 = c._sock
    c.spawn()  # second call → no new connection
    sock2 = c._sock
    assert sock1 is sock2
    c.shutdown()


# ---------------------------------------------------------------------------
# initialize
# ---------------------------------------------------------------------------

def test_initialize_returns_capabilities(mock_uds: str) -> None:
    c = _make_client(mock_uds)
    c.spawn()
    result = c.initialize()
    assert isinstance(result, dict)
    assert result.get("protocolVersion") == "2024-11-05"
    assert "serverInfo" in result
    c.shutdown()


# ---------------------------------------------------------------------------
# list_tools
# ---------------------------------------------------------------------------

def test_list_tools_returns_tool_definitions(mock_uds: str) -> None:
    c = _make_client(mock_uds)
    c.spawn()
    c.initialize()
    tools = c.list_tools()
    assert isinstance(tools, list)
    assert len(tools) >= 1
    assert tools[0]["name"] == "echo"
    c.shutdown()


# ---------------------------------------------------------------------------
# call_tool
# ---------------------------------------------------------------------------

def test_call_tool_roundtrips_args(mock_uds: str) -> None:
    c = _make_client(mock_uds)
    c.spawn()
    c.initialize()
    result = c.call_tool("echo", {"message": "hello"})
    assert isinstance(result, dict)
    content = result.get("content", [])
    assert any("hello" in str(c2) for c2 in content)
    c.shutdown()


# ---------------------------------------------------------------------------
# Server-side error
# ---------------------------------------------------------------------------

@_REQUIRES_AF_UNIX
def test_call_tool_raises_mcp_server_error() -> None:
    """Mock with MOCK_MCP_TOOL_ERROR=1 returns JSON-RPC error → client raises."""
    sock_path = f"/tmp/st-err-{uuid.uuid4().hex[:8]}.sock"
    env = os.environ.copy()
    env["MOCK_MCP_TOOL_ERROR"] = "1"
    proc = subprocess.Popen([sys.executable, MOCK_SERVER, sock_path], env=env)
    deadline = time.time() + 5
    while time.time() < deadline and not os.path.exists(sock_path):
        time.sleep(0.05)
    try:
        c = _make_client(sock_path)
        c.spawn()
        c.initialize()
        with pytest.raises(MCPServerError) as exc_info:
            c.call_tool("echo", {})
        assert exc_info.value.code == -32000
        assert "tool execution failed" in str(exc_info.value)
        c.shutdown()
    finally:
        proc.terminate()
        try: proc.wait(timeout=3)
        except subprocess.TimeoutExpired: proc.kill()


# ---------------------------------------------------------------------------
# shutdown
# ---------------------------------------------------------------------------

def test_shutdown_closes_socket(mock_uds: str) -> None:
    c = _make_client(mock_uds)
    c.spawn()
    assert c.is_alive()
    c.shutdown()
    assert not c.is_alive()


def test_shutdown_is_idempotent(mock_uds: str) -> None:
    c = _make_client(mock_uds)
    c.spawn()
    c.shutdown()
    c.shutdown()  # must not raise


# ---------------------------------------------------------------------------
# _mcp_call helper
# ---------------------------------------------------------------------------

def test_mcp_call_returns_none_when_server_not_in_registry() -> None:
    result = _mcp_call("nonexistent-server", "echo", {"x": 1})
    assert result is None


def test_mcp_call_uses_registered_client(mock_uds: str) -> None:
    c = _make_client(mock_uds)
    c.spawn(); c.initialize()
    _mcp_register("_test_call", c)
    try:
        result = _mcp_call("_test_call", "echo", {"message": "lazy"})
        assert result is not None
        content = result.get("content", [])
        assert any("lazy" in str(item) for item in content)
    finally:
        with _MCP_LOCK:
            _MCP_SERVERS.pop("_test_call", None)
        c.shutdown()


# ---------------------------------------------------------------------------
# _mcp_get_server — dead client returns None
# ---------------------------------------------------------------------------

def test_mcp_get_server_removes_dead_client(mock_uds: str) -> None:
    c = _make_client(mock_uds)
    c.spawn()
    c.shutdown()  # close → not alive
    with _MCP_LOCK:
        _MCP_SERVERS["_test_dead"] = c
    try:
        result = _mcp_get_server("_test_dead")
        assert result is None
        with _MCP_LOCK:
            assert "_test_dead" not in _MCP_SERVERS
    finally:
        with _MCP_LOCK:
            _MCP_SERVERS.pop("_test_dead", None)


# ---------------------------------------------------------------------------
# _mcp_register + _mcp_call
# ---------------------------------------------------------------------------

def test_mcp_register_allows_mcp_call(mock_uds: str) -> None:
    c = _make_client(mock_uds)
    c.spawn(); c.initialize()
    _mcp_register("_test_register", c)
    try:
        result = _mcp_call("_test_register", "echo", {"message": "registered"})
        assert result is not None
        content = result.get("content", [])
        assert any("registered" in str(item) for item in content)
    finally:
        with _MCP_LOCK:
            _MCP_SERVERS.pop("_test_register", None)
        c.shutdown()


# ---------------------------------------------------------------------------
# Thread-safe _next_id
# ---------------------------------------------------------------------------

def test_next_id_unique_across_threads() -> None:
    c = MCPClient(name="x", timeout=5, socket_path="/dev/null")
    ids: list[int] = []
    ids_lock = threading.Lock()

    def collect() -> None:
        for _ in range(1000):
            with ids_lock:
                ids.append(c._next_id())

    t1 = threading.Thread(target=collect)
    t2 = threading.Thread(target=collect)
    t1.start(); t2.start()
    t1.join(); t2.join()
    assert len(ids) == 2000
    assert len(set(ids)) == 2000


# ---------------------------------------------------------------------------
# Auto-spawn connect timeout (PR #134)
# ---------------------------------------------------------------------------

def test_connect_timeout_default_is_60s() -> None:
    """The retry budget for auto-spawn-then-connect is 60s by default.

    Cold-starting cclsp+intelephense on a large repo (600K LOC) takes 30-60s.
    The previous 7.5s budget gave up before the daemon bound its socket.
    """
    assert MCPClient._CONNECT_TIMEOUT_SECONDS == 60


@_REQUIRES_AF_UNIX
def test_connect_timeout_env_override(tmp_path: Path, monkeypatch) -> None:
    """SUPERTOOL_MCP_CONNECT_TIMEOUT overrides the default budget."""
    nonexistent = f"/tmp/st-timeout-{uuid.uuid4().hex[:8]}.sock"
    c = MCPClient(name="x", timeout=1, socket_path=nonexistent)
    monkeypatch.setenv("SUPERTOOL_MCP_CONNECT_TIMEOUT", "0.5")
    start = time.time()
    with pytest.raises(MCPServerError):
        c.spawn()
    elapsed = time.time() - start
    # Should give up around 0.5s, not the 60s default
    assert elapsed < 5.0, f"timeout override ignored: spent {elapsed:.2f}s"


@_REQUIRES_AF_UNIX
def test_connect_error_mentions_stderr_log_when_present(tmp_path: Path, monkeypatch) -> None:
    """When the daemon wrote a stderr log, error points the user at it."""
    sock = f"/tmp/st-broken-{uuid.uuid4().hex[:8]}.sock"
    stderr_log = sock + ".stderr"
    Path(stderr_log).write_text("cclsp: command not found\n")
    c = MCPClient(name="lsp", timeout=1, socket_path=sock)
    monkeypatch.setenv("SUPERTOOL_MCP_CONNECT_TIMEOUT", "0.3")
    with pytest.raises(MCPServerError) as exc_info:
        c.spawn()
    msg = str(exc_info.value)
    assert stderr_log in msg
    assert "startup errors" in msg or "stderr" in msg


@_REQUIRES_AF_UNIX
def test_connect_error_hints_path_when_no_stderr_log(tmp_path: Path, monkeypatch) -> None:
    """No stderr log → error hints that the configured cmd may not be on PATH."""
    sock = f"/tmp/st-broken-{uuid.uuid4().hex[:8]}.sock"  # no .stderr companion
    c = MCPClient(name="lsp", timeout=1, socket_path=sock)
    monkeypatch.setenv("SUPERTOOL_MCP_CONNECT_TIMEOUT", "0.3")
    with pytest.raises(MCPServerError) as exc_info:
        c.spawn()
    msg = str(exc_info.value)
    assert "PATH" in msg or "cmd" in msg
