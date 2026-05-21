"""Tests for MCP config + extension routing.

The MCP client talks to a UDS daemon. Tests use a UDS-server mock fixture spawned per
test with a tmp socket path injected directly into the spec — bypasses the auto-spawn
path while exercising the full NDJSON wire protocol.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

import supertool
from supertool import (
    MCPClient, _mcp_route, _mcp_ensure_server,
    _extract_path_from_mcp_result, _MCP_SERVERS, _MCP_LOCK,
    op_resolve,
)

MOCK_SERVER = str(Path(__file__).parent / "fixtures" / "mock_mcp_server.py")


def _set_mcp_specs(specs: dict) -> None:
    """Inject MCP specs into the module-level _mcp_specs dict."""
    supertool._mcp_specs.clear()
    supertool._mcp_specs.update(specs)


@pytest.fixture
def mock_uds():
    """Spawn the UDS-mode mock MCP server on a per-test socket path.

    Sockets live in /tmp/ (macOS AF_UNIX path limit ~104 chars; pytest tmp_path is too deep).
    """
    sock_path = f"/tmp/st-mock-{uuid.uuid4().hex[:8]}.sock"
    proc = subprocess.Popen([sys.executable, MOCK_SERVER, sock_path])
    # Wait for the server to bind the socket
    deadline = time.time() + 5
    while time.time() < deadline and not os.path.exists(sock_path):
        time.sleep(0.05)
    if not os.path.exists(sock_path):
        proc.terminate()
        raise RuntimeError(f"mock MCP server did not bind {sock_path}")
    try:
        yield sock_path
    finally:
        proc.terminate()
        try: proc.wait(timeout=3)
        except subprocess.TimeoutExpired: proc.kill()
        if os.path.exists(sock_path):
            try: os.unlink(sock_path)
            except OSError: pass


def test_route_matches_by_extension_glob(mock_uds: str) -> None:
    _set_mcp_specs({
        "php-lsp": {"match": "*.php", "socket_path": mock_uds,
                    "tools": {"resolve": "definition"}}
    })
    assert _mcp_route("foo.php", "resolve") == ("php-lsp", "definition")


def test_route_returns_none_when_no_match(mock_uds: str) -> None:
    _set_mcp_specs({
        "php-lsp": {"match": "*.php", "socket_path": mock_uds,
                    "tools": {"resolve": "definition"}}
    })
    assert _mcp_route("foo.py", "resolve") is None


def test_route_returns_none_when_op_not_in_tools(mock_uds: str) -> None:
    _set_mcp_specs({
        "php-lsp": {"match": "*.php", "socket_path": mock_uds,
                    "tools": {"resolve": "definition"}}
    })
    assert _mcp_route("foo.php", "refs") is None


def test_ensure_server_connects_on_demand(mock_uds: str) -> None:
    _set_mcp_specs({
        "lsp-test": {"match": "*.php", "socket_path": mock_uds,
                     "tools": {"resolve": "definition"}}
    })
    try:
        srv = _mcp_ensure_server("lsp-test")
        assert srv is not None
        assert srv.is_alive()
        # Second call returns same instance (cached)
        srv2 = _mcp_ensure_server("lsp-test")
        assert srv2 is srv
    finally:
        with _MCP_LOCK:
            srv = _MCP_SERVERS.pop("lsp-test", None)
        if srv:
            srv.shutdown()


def test_ensure_server_returns_none_on_bad_socket(tmp_path: Path) -> None:
    """Socket_path that doesn't exist → connect fails → ensure returns None."""
    _set_mcp_specs({
        "broken": {"match": "*.php", "socket_path": str(tmp_path / "nope.sock"),
                   "tools": {"resolve": "definition"}}
    })
    srv = _mcp_ensure_server("broken")
    assert srv is None


def test_extract_path_from_text_content() -> None:
    result = {"content": [{"type": "text", "text": "/path/to/Foo.php"}]}
    assert _extract_path_from_mcp_result(result) == "/path/to/Foo.php"


def test_extract_path_from_file_uri() -> None:
    result = {"uri": "file:///path/to/Foo.php"}
    assert _extract_path_from_mcp_result(result) == "/path/to/Foo.php"


def test_extract_path_returns_none_for_empty() -> None:
    assert _extract_path_from_mcp_result({}) is None
    assert _extract_path_from_mcp_result(None) is None


def test_extract_path_from_file_uri_text_content() -> None:
    """file:// in text-content shape must not corrupt the path (was returning //path)."""
    result = {"content": [{"type": "text", "text": "file:///path/to/Foo.php"}]}
    assert _extract_path_from_mcp_result(result) == "/path/to/Foo.php"


def test_extract_path_from_file_uri_url_decode() -> None:
    """Percent-encoded characters in file:// URIs are decoded."""
    result = {"content": [{"type": "text", "text": "file:///path%20with%20space.php"}]}
    assert _extract_path_from_mcp_result(result) == "/path with space.php"


def test_op_resolve_uses_mcp_when_configured(tmp_path: Path, mock_uds: str) -> None:
    """op_resolve delegates to the MCP daemon when a matching spec is configured."""
    php_file = str(tmp_path / "bar.php")
    _set_mcp_specs({
        "php-lsp": {"match": "*.php", "socket_path": mock_uds,
                    "tools": {"resolve": "definition"}}
    })
    srv_name = "php-lsp"
    try:
        result = op_resolve("Foo", from_file=php_file)
        # Mock returns file_path as the resolved path — proves MCP was hit
        assert php_file in result
        assert "→" in result
    finally:
        with _MCP_LOCK:
            srv = _MCP_SERVERS.pop(srv_name, None)
        if srv:
            srv.shutdown()
        supertool._mcp_specs.clear()


def test_op_resolve_falls_back_to_heuristic_on_mcp_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP failure (bad socket) silently falls through to heuristic glob."""
    monkeypatch.chdir(tmp_path)
    _set_mcp_specs({
        "broken-lsp": {"match": "*.php", "socket_path": str(tmp_path / "nope.sock"),
                       "tools": {"resolve": "definition"}}
    })
    php_file = str(tmp_path / "bar.php")
    try:
        result = op_resolve("DoesNotExist", from_file=php_file)
        # Must not raise; heuristic returns "not found"
        assert "not found" in result or "→" in result
    finally:
        supertool._mcp_specs.clear()


def test_cli_resolve_forwards_from_file_to_mcp(
    tmp_path: Path, mock_uds: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """`resolve:SYMBOL:FILE` CLI form must forward FILE so the MCP route triggers.

    Regression: dispatcher previously dropped parts[2], so from_file=None and MCP never hit.
    """
    php_file = str(tmp_path / "bar.php")
    _set_mcp_specs({
        "php-lsp": {"match": "*.php", "socket_path": mock_uds,
                    "tools": {"resolve": "definition"}}
    })
    srv_name = "php-lsp"
    try:
        rc = supertool.main([f"resolve:Foo:{php_file}"])
        out = capsys.readouterr().out
        assert php_file in out
        assert rc == 0
    finally:
        with _MCP_LOCK:
            srv = _MCP_SERVERS.pop(srv_name, None)
        if srv:
            srv.shutdown()
        supertool._mcp_specs.clear()
