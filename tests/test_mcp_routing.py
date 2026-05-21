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
