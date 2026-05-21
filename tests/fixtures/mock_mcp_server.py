#!/usr/bin/env python3
"""Minimal MCP server fixture for test_mcp_client.py.

Speaks JSON-RPC 2.0 over stdio with Content-Length framing.
Behaviour is controlled via environment variables:
  MOCK_MCP_HANG=1        — sleep forever on tools/call (triggers MCPTimeout)
  MOCK_MCP_TOOL_ERROR=1  — return a JSON-RPC error on tools/call
"""
from __future__ import annotations

import json
import os
import sys
import time

TOOLS = [
    {
        "name": "echo",
        "description": "Echo back args",
        "inputSchema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
        },
    },
    {
        "name": "definition",
        "description": "Mock LSP-style definition lookup",
        "inputSchema": {"type": "object",
                        "properties": {"symbol": {"type": "string"},
                                       "file": {"type": "string"}}},
    },
]


def send(payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
    sys.stdout.buffer.write(header + body)
    sys.stdout.buffer.flush()


def recv() -> dict:
    content_length = 0
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            sys.exit(0)
        line = line.decode("utf-8").rstrip("\r\n")
        if line == "":
            break
        if line.lower().startswith("content-length:"):
            content_length = int(line.split(":", 1)[1].strip())
    raw = sys.stdin.buffer.read(content_length)
    return json.loads(raw.decode("utf-8"))


def handle(msg: dict) -> None:
    method = msg.get("method", "")
    msg_id = msg.get("id")

    # Notifications have no id — no response needed
    if msg_id is None:
        return

    if method == "initialize":
        send({
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mock-mcp", "version": "0.0.1"},
            },
        })
    elif method == "tools/list":
        send({
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"tools": TOOLS},
        })
    elif method == "tools/call":
        if os.environ.get("MOCK_MCP_HANG") == "1":
            time.sleep(9999)
            return
        if os.environ.get("MOCK_MCP_TOOL_ERROR") == "1":
            send({
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32000, "message": "tool execution failed"},
            })
            return
        params = msg.get("params", {})
        tool_name = params.get("name", "")
        args = params.get("arguments", {})
        if tool_name == "definition":
            send({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"content": [{"type": "text",
                                        "text": args.get("file", "/mock/resolved.php")}]},
            })
            return
        send({
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"content": [{"type": "text", "text": json.dumps(args)}]},
        })
    else:
        send({
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        })


def main() -> None:
    while True:
        try:
            msg = recv()
        except (EOFError, json.JSONDecodeError):
            break
        handle(msg)


if __name__ == "__main__":
    main()
