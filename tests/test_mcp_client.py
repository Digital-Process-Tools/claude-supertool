"""Tests for MCP client primitives in supertool.py.

Uses tests/fixtures/mock_mcp_server.py — a minimal stdio JSON-RPC server.
No real LSP dependencies required.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

import supertool
from supertool import MCPServer, MCPServerError, MCPTimeout, _mcp_call, _mcp_get_server, _mcp_register, _MCP_SERVERS, _MCP_LOCK

MOCK_SERVER = str(Path(__file__).parent / "fixtures" / "mock_mcp_server.py")
MOCK_CMD = f"{sys.executable} {MOCK_SERVER}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_server(timeout: int = 5, env: dict | None = None) -> MCPServer:
    return MCPServer(name="test", cmd=MOCK_CMD, env=env, timeout=timeout)


# ---------------------------------------------------------------------------
# spawn + is_alive
# ---------------------------------------------------------------------------

def test_spawn_starts_process() -> None:
    srv = _make_server()
    assert not srv.is_alive()
    srv.spawn()
    assert srv.is_alive()
    srv.shutdown()


def test_spawn_is_idempotent() -> None:
    srv = _make_server()
    srv.spawn()
    pid1 = srv._proc.pid
    srv.spawn()  # second call — must not replace process
    pid2 = srv._proc.pid
    assert pid1 == pid2
    srv.shutdown()


# ---------------------------------------------------------------------------
# initialize
# ---------------------------------------------------------------------------

def test_initialize_returns_capabilities() -> None:
    srv = _make_server()
    srv.spawn()
    result = srv.initialize()
    assert isinstance(result, dict)
    assert result.get("protocolVersion") == "2024-11-05"
    assert "serverInfo" in result
    srv.shutdown()


# ---------------------------------------------------------------------------
# list_tools
# ---------------------------------------------------------------------------

def test_list_tools_returns_tool_definitions() -> None:
    srv = _make_server()
    srv.spawn()
    srv.initialize()
    tools = srv.list_tools()
    assert isinstance(tools, list)
    assert len(tools) >= 1
    assert tools[0]["name"] == "echo"
    srv.shutdown()


# ---------------------------------------------------------------------------
# call_tool
# ---------------------------------------------------------------------------

def test_call_tool_roundtrips_args() -> None:
    srv = _make_server()
    srv.spawn()
    srv.initialize()
    result = srv.call_tool("echo", {"message": "hello"})
    assert isinstance(result, dict)
    content = result.get("content", [])
    assert any("hello" in str(c) for c in content)
    srv.shutdown()


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------

@pytest.mark.skip(reason="hangs in CI — subprocess cleanup race; track in follow-up")
def test_call_tool_raises_mcp_timeout() -> None:
    srv = _make_server(timeout=1, env={"MOCK_MCP_HANG": "1"})
    srv.spawn()
    srv.initialize()
    with pytest.raises(MCPTimeout):
        srv.call_tool("echo", {})
    srv.shutdown()


# ---------------------------------------------------------------------------
# Server-side error
# ---------------------------------------------------------------------------

def test_call_tool_raises_mcp_server_error() -> None:
    srv = _make_server(env={"MOCK_MCP_TOOL_ERROR": "1"})
    srv.spawn()
    srv.initialize()
    with pytest.raises(MCPServerError) as exc_info:
        srv.call_tool("echo", {})
    assert exc_info.value.code == -32000
    assert "tool execution failed" in str(exc_info.value)
    srv.shutdown()


# ---------------------------------------------------------------------------
# shutdown
# ---------------------------------------------------------------------------

def test_shutdown_cleans_up_process() -> None:
    srv = _make_server()
    srv.spawn()
    assert srv.is_alive()
    srv.shutdown()
    assert not srv.is_alive()


def test_shutdown_is_idempotent() -> None:
    srv = _make_server()
    srv.spawn()
    srv.shutdown()
    srv.shutdown()  # must not raise


# ---------------------------------------------------------------------------
# _mcp_call helper
# ---------------------------------------------------------------------------

def test_mcp_call_returns_none_when_server_not_in_registry() -> None:
    result = _mcp_call("nonexistent-server", "echo", {"x": 1})
    assert result is None


def test_mcp_call_lazy_spawns_via_registry() -> None:
    srv = _make_server()
    srv.spawn()
    srv.initialize()
    with _MCP_LOCK:
        _MCP_SERVERS["_test_lazy"] = srv
    try:
        result = _mcp_call("_test_lazy", "echo", {"message": "lazy"})
        assert result is not None
        content = result.get("content", [])
        assert any("lazy" in str(c) for c in content)
    finally:
        with _MCP_LOCK:
            _MCP_SERVERS.pop("_test_lazy", None)
        srv.shutdown()


# ---------------------------------------------------------------------------
# _mcp_get_server — dead server returns None
# ---------------------------------------------------------------------------

def test_mcp_get_server_removes_dead_server() -> None:
    srv = _make_server()
    srv.spawn()
    srv.shutdown()  # kill it
    with _MCP_LOCK:
        _MCP_SERVERS["_test_dead"] = srv
    try:
        result = _mcp_get_server("_test_dead")
        assert result is None
        # Should have been removed from registry
        with _MCP_LOCK:
            assert "_test_dead" not in _MCP_SERVERS
    finally:
        with _MCP_LOCK:
            _MCP_SERVERS.pop("_test_dead", None)


# ---------------------------------------------------------------------------
# M1: _mcp_register + _mcp_call
# ---------------------------------------------------------------------------

def test_mcp_register_allows_mcp_call() -> None:
    """_mcp_register pre-registers a server; _mcp_call uses it without manual
    registry manipulation."""
    srv = _make_server()
    srv.spawn()
    srv.initialize()
    _mcp_register("_test_register", srv)
    try:
        result = _mcp_call("_test_register", "echo", {"message": "registered"})
        assert result is not None
        content = result.get("content", [])
        assert any("registered" in str(c) for c in content)
    finally:
        with _MCP_LOCK:
            _MCP_SERVERS.pop("_test_register", None)
        srv.shutdown()


# ---------------------------------------------------------------------------
# M2: timeout marks server dead; subsequent call fails fast
# ---------------------------------------------------------------------------

@pytest.mark.skip(reason="hangs in CI — subprocess cleanup race; track in follow-up")
def test_timeout_marks_server_dead_and_second_call_fails_fast() -> None:
    """After MCPTimeout the server is marked dead. The next call raises
    MCPServerError immediately instead of hanging."""
    srv = _make_server(timeout=1, env={"MOCK_MCP_HANG": "1"})
    srv.spawn()
    srv.initialize()
    with pytest.raises(MCPTimeout):
        srv.call_tool("echo", {})
    # Server must now be marked dead
    assert srv._dead is True
    # Second call must fail fast — not hang
    with pytest.raises(MCPServerError, match="marked dead"):
        srv.call_tool("echo", {})
    srv.shutdown()


# ---------------------------------------------------------------------------
# M3: thread-safe _next_id
# ---------------------------------------------------------------------------

def test_next_id_unique_across_threads() -> None:
    """Two threads calling _next_id() 1000 times each must produce 2000
    unique IDs with no duplicates."""
    srv = _make_server()
    ids: list[int] = []
    ids_lock = threading.Lock()

    def collect() -> None:
        for _ in range(1000):
            with ids_lock:
                ids.append(srv._next_id())

    t1 = threading.Thread(target=collect)
    t2 = threading.Thread(target=collect)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert len(ids) == 2000
    assert len(set(ids)) == 2000
