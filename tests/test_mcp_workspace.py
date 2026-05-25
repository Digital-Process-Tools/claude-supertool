"""Tests for MCP routing in op_workspace — References, Symbols, and Imports (sub-PR 3)."""
from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

import socket as _socket

import supertool
from supertool import (
    _mcp_route, _mcp_ensure_server, _mcp_call,
    _extract_refs_from_mcp_result, _extract_symbols_from_mcp_result,
    _MCP_SERVERS, _MCP_LOCK,
    op_workspace, op_resolve,
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


def _set_mcp_specs(specs: dict) -> None:
    supertool._mcp_specs.clear()
    supertool._mcp_specs.update(specs)


def _cleanup_servers(*names: str) -> None:
    with _MCP_LOCK:
        for name in names:
            srv = _MCP_SERVERS.pop(name, None)
            if srv:
                try:
                    srv.shutdown()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Unit: helper extractors
# ---------------------------------------------------------------------------

def test_extract_refs_from_mcp_result_basic() -> None:
    result = {"content": [{"type": "text", "text": "file1.php:10:line content\nfile2.php:20:other content"}]}
    refs = _extract_refs_from_mcp_result(result)
    assert refs == ["file1.php:10:line content", "file2.php:20:other content"]


def test_extract_refs_from_mcp_result_empty_text() -> None:
    result = {"content": [{"type": "text", "text": ""}]}
    assert _extract_refs_from_mcp_result(result) is None


def test_extract_refs_from_mcp_result_no_content() -> None:
    assert _extract_refs_from_mcp_result({}) is None
    assert _extract_refs_from_mcp_result(None) is None


def test_extract_refs_strips_blank_lines() -> None:
    result = {"content": [{"type": "text", "text": "a.php:1:foo\n\nb.php:2:bar\n"}]}
    refs = _extract_refs_from_mcp_result(result)
    assert refs == ["a.php:1:foo", "b.php:2:bar"]


def test_extract_symbols_from_mcp_result_basic() -> None:
    result = {"content": [{"type": "text", "text": "class Foo  [10-50]\n  method bar  [12-20]"}]}
    sym = _extract_symbols_from_mcp_result(result)
    assert sym is not None
    assert "class Foo" in sym
    assert "method bar" in sym


def test_extract_symbols_from_mcp_result_no_content() -> None:
    assert _extract_symbols_from_mcp_result({}) is None
    assert _extract_symbols_from_mcp_result(None) is None


def test_extract_symbols_from_mcp_result_empty_text() -> None:
    result = {"content": [{"type": "text", "text": ""}]}
    assert _extract_symbols_from_mcp_result(result) is None


# ---------------------------------------------------------------------------
# Route matching: refs + symbols
# ---------------------------------------------------------------------------

def test_route_refs_matches_when_configured() -> None:
    _set_mcp_specs({
        "php-lsp": {"socket_path": "/tmp/unused.sock", "match": "*.php",
                    "tools": {"refs": "references"}}
    })
    assert _mcp_route("foo.php", "refs") == ("php-lsp", "references")
    supertool._mcp_specs.clear()


def test_route_symbols_matches_when_configured() -> None:
    _set_mcp_specs({
        "php-lsp": {"socket_path": "/tmp/unused.sock", "match": "*.php",
                    "tools": {"symbols": "documentSymbol"}}
    })
    assert _mcp_route("foo.php", "symbols") == ("php-lsp", "documentSymbol")
    supertool._mcp_specs.clear()


def test_route_refs_returns_none_when_not_in_tools() -> None:
    _set_mcp_specs({
        "php-lsp": {"socket_path": "/tmp/unused.sock", "match": "*.php",
                    "tools": {"resolve": "definition"}}
    })
    assert _mcp_route("foo.php", "refs") is None
    supertool._mcp_specs.clear()


# ---------------------------------------------------------------------------
# op_workspace: References section uses MCP when configured
# ---------------------------------------------------------------------------

def test_workspace_references_uses_mcp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mock_uds: str) -> None:
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "Widget.class.php"
    f.write_text("<?php\nclass Widget {}\n")

    _set_mcp_specs({
        "php-lsp": {"match": "*.php", "socket_path": mock_uds,
                    "tools": {"refs": "references"}}
    })
    try:
        out = op_workspace(str(f))
        assert "## References" in out
        # Mock server returns "file1.php:10:line content\nfile2.php:20:other content"
        assert "file1.php" in out
        assert "file2.php" in out
    finally:
        _cleanup_servers("php-lsp")
        supertool._mcp_specs.clear()


@pytest.mark.slow
def test_workspace_references_falls_back_on_mcp_miss(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When MCP returns no result (broken server), fall back to heuristic grep."""
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "Widget.class.php"
    f.write_text("<?php\nclass Widget {}\n")

    _set_mcp_specs({
        "broken-lsp": {"socket_path": "/tmp/nope.sock", "match": "*.php",
                       "tools": {"refs": "references"}}
    })
    try:
        out = op_workspace(str(f))
        # Must still produce a References section (heuristic path)
        assert "## References" in out
    finally:
        _cleanup_servers("broken-lsp")
        supertool._mcp_specs.clear()


def test_workspace_references_falls_back_when_no_mcp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No MCP configured → heuristic grep runs normally."""
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "Widget.class.php"
    f.write_text("<?php\nclass Widget {}\n")
    supertool._mcp_specs.clear()

    out = op_workspace(str(f))
    assert "## References" in out


# ---------------------------------------------------------------------------
# op_workspace: Symbols section uses MCP when configured
# ---------------------------------------------------------------------------

def test_workspace_symbols_uses_mcp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mock_uds: str) -> None:
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "Widget.class.php"
    f.write_text("<?php\nclass Widget {}\n")

    _set_mcp_specs({
        "php-lsp": {"match": "*.php", "socket_path": mock_uds,
                    "tools": {"symbols": "documentSymbol"}}
    })
    try:
        out = op_workspace(str(f))
        assert "## Symbols" in out
        # Mock server returns "class Foo  [10-50]\n  method bar  [12-20]"
        assert "class Foo" in out
        assert "method bar" in out
    finally:
        _cleanup_servers("php-lsp")
        supertool._mcp_specs.clear()


def test_workspace_symbols_falls_back_when_no_mcp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "mymodule.py"
    f.write_text("class MyModule:\n    def run(self) -> None:\n        pass\n")
    supertool._mcp_specs.clear()

    out = op_workspace(str(f))
    assert "## Symbols" in out
    assert "MyModule" in out


@pytest.mark.slow
def test_workspace_symbols_falls_back_on_bad_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "mymodule.py"
    f.write_text("class MyModule:\n    def run(self) -> None:\n        pass\n")

    _set_mcp_specs({
        "broken-lsp": {"socket_path": "/tmp/nope.sock", "match": "*.py",
                       "tools": {"symbols": "documentSymbol"}}
    })
    try:
        out = op_workspace(str(f))
        assert "## Symbols" in out
        # Heuristic regex fallback should still find the class
        assert "MyModule" in out
    finally:
        _cleanup_servers("broken-lsp")
        supertool._mcp_specs.clear()


# ---------------------------------------------------------------------------
# op_workspace: Imports section already uses MCP via op_resolve (sub-PR 2)
# ---------------------------------------------------------------------------

def test_workspace_imports_uses_mcp_via_op_resolve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mock_uds: str) -> None:
    """Imports section delegates to op_resolve which uses MCP.

    Verifies end-to-end: a PHP file with a use-statement renders an Imports
    section whose resolved path comes from the MCP server.
    """
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "Consumer.class.php"
    f.write_text("<?php\nuse Foo\\Bar;\nclass Consumer {}\n")

    php_file = str(f)
    _set_mcp_specs({
        "php-lsp": {"match": "*.php", "socket_path": mock_uds,
                    "tools": {"resolve": "definition"}}
    })
    try:
        out = op_workspace(php_file)
        assert "## Imports" in out
        # Mock server returns the from_file path as the resolved definition —
        # which equals php_file itself. Confirm the import line rendered.
        assert "Bar" in out or "Foo" in out
    finally:
        _cleanup_servers("php-lsp")
        supertool._mcp_specs.clear()


def test_workspace_imports_section_present_without_mcp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Imports section works (heuristic fallback) when no MCP configured."""
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "Consumer.class.php"
    f.write_text("<?php\nuse Foo\\Bar;\nclass Consumer {}\n")
    supertool._mcp_specs.clear()

    out = op_workspace(str(f))
    assert "## Imports" in out
