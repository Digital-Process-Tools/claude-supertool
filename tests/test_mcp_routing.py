"""Tests for MCP config + extension routing (sub-PR 2)."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import supertool
from supertool import (
    MCPServer, _mcp_route, _mcp_ensure_server,
    _extract_path_from_mcp_result, _MCP_SERVERS, _MCP_LOCK,
    op_resolve,
)

MOCK_SERVER = str(Path(__file__).parent / "fixtures" / "mock_mcp_server.py")
MOCK_CMD = f"{sys.executable} {MOCK_SERVER}"


def _set_mcp_specs(specs: dict) -> None:
    """Inject MCP specs into the module-level _mcp_specs dict."""
    supertool._mcp_specs.clear()
    supertool._mcp_specs.update(specs)


def test_route_matches_by_extension_glob() -> None:
    _set_mcp_specs({
        "php-lsp": {"cmd": MOCK_CMD, "match": "*.php",
                    "tools": {"resolve": "definition"}}
    })
    assert _mcp_route("foo.php", "resolve") == ("php-lsp", "definition")


def test_route_returns_none_when_no_match() -> None:
    _set_mcp_specs({
        "php-lsp": {"cmd": MOCK_CMD, "match": "*.php",
                    "tools": {"resolve": "definition"}}
    })
    assert _mcp_route("foo.py", "resolve") is None


def test_route_returns_none_when_op_not_in_tools() -> None:
    _set_mcp_specs({
        "php-lsp": {"cmd": MOCK_CMD, "match": "*.php", "tools": {"resolve": "definition"}}
    })
    assert _mcp_route("foo.php", "refs") is None


def test_ensure_server_spawns_on_demand() -> None:
    _set_mcp_specs({
        "lsp-test": {"cmd": MOCK_CMD, "match": "*.php",
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


def test_ensure_server_returns_none_on_bad_cmd() -> None:
    _set_mcp_specs({
        "broken": {"cmd": "/nonexistent/binary", "match": "*.php",
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


def test_op_resolve_uses_mcp_when_configured(tmp_path: Path) -> None:
    """op_resolve delegates to MCP server when a matching spec is configured."""
    php_file = str(tmp_path / "bar.php")
    _set_mcp_specs({
        "php-lsp": {"cmd": MOCK_CMD, "match": "*.php",
                    "tools": {"resolve": "definition"}}
    })
    srv_name = "php-lsp"
    try:
        result = op_resolve("Foo", from_file=php_file)
        # Mock server returns the from_file value as the resolved path
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
    """MCP failure (bad cmd) silently falls through to heuristic glob."""
    monkeypatch.chdir(tmp_path)
    _set_mcp_specs({
        "broken-lsp": {"cmd": "/nonexistent/binary", "match": "*.php",
                       "tools": {"resolve": "definition"}}
    })
    php_file = str(tmp_path / "bar.php")
    try:
        result = op_resolve("DoesNotExist", from_file=php_file)
        # Must not raise; heuristic returns "not found"
        assert "not found" in result or "→" in result
    finally:
        supertool._mcp_specs.clear()
