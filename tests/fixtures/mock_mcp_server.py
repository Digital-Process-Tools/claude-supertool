#!/usr/bin/env python3
"""Minimal MCP server fixture for MCP client/routing tests.

Listens on a Unix socket (path passed as argv[1]) and speaks NDJSON JSON-RPC 2.0 —
the same wire format the official MCP Python SDK uses over stdio. Each request is one
JSON line terminated by `\n`. Responses likewise.

Behaviour controlled via env vars:
  MOCK_MCP_HANG=1        — sleep forever on tools/call (triggers MCPTimeout)
  MOCK_MCP_TOOL_ERROR=1  — return a JSON-RPC error on tools/call
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time

TOOLS = [
    {
        "name": "echo",
        "description": "Echo back args",
        "inputSchema": {"type": "object",
                        "properties": {"message": {"type": "string"}}},
    },
    {
        "name": "definition",
        "description": "Mock LSP-style definition lookup",
        "inputSchema": {"type": "object",
                        "properties": {"symbol_name": {"type": "string"},
                                       "file_path": {"type": "string"}}},
    },
    {
        "name": "references",
        "description": "Mock LSP-style references lookup",
        "inputSchema": {"type": "object",
                        "properties": {"symbol_name": {"type": "string"},
                                       "file_path": {"type": "string"}}},
    },
    {
        "name": "documentSymbol",
        "description": "Mock LSP-style document symbol listing",
        "inputSchema": {"type": "object",
                        "properties": {"file_path": {"type": "string"}}},
    },
]


def handle_request(msg: dict) -> dict | None:
    method = msg.get("method", "")
    msg_id = msg.get("id")
    if msg_id is None:
        return None  # notification

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "mock-mcp", "version": "0.0.1"},
        }}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        if os.environ.get("MOCK_MCP_HANG") == "1":
            time.sleep(9999)
            return None
        if os.environ.get("MOCK_MCP_TOOL_ERROR") == "1":
            return {"jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32000, "message": "tool execution failed"}}
        params = msg.get("params", {})
        tool_name = params.get("name", "")
        args = params.get("arguments", {})
        if tool_name == "definition":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": [
                {"type": "text", "text": args.get("file_path", "/mock/resolved.php")}]}}
        if tool_name == "references":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": [
                {"type": "text", "text": "file1.php:10:line content\nfile2.php:20:other content"}]}}
        if tool_name == "documentSymbol":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": [
                {"type": "text", "text": "class Foo  [10-50]\n  method bar  [12-20]"}]}}
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": [
            {"type": "text", "text": json.dumps(args)}]}}
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def serve_client(client_sock: socket.socket) -> None:
    f = client_sock.makefile("rwb", buffering=0)
    try:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            resp = handle_request(msg)
            if resp is not None:
                f.write((json.dumps(resp) + "\n").encode("utf-8"))
    except OSError:
        pass
    finally:
        try: f.close()
        except OSError: pass


def main(argv: list) -> int:
    if len(argv) < 2:
        sys.stderr.write("usage: mock_mcp_server.py SOCKET_PATH\n")
        return 2
    sock_path = argv[1]
    try: os.unlink(sock_path)
    except FileNotFoundError: pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(8)
    try:
        while True:
            client, _ = server.accept()
            serve_client(client)
            try: client.close()
            except OSError: pass
    except KeyboardInterrupt:
        pass
    finally:
        try: server.close()
        except OSError: pass
        try: os.unlink(sock_path)
        except FileNotFoundError: pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
