#!/usr/bin/env python3










from __future__ import annotations

import hashlib
import json
import os
import random
import socket
import sys
import time

DAEMON_NAME = os.environ.get("MCP_PHPSTAN_DAEMON_NAME", "phpstan-warm")
DAEMON_PROC = os.environ.get("MCP_PHPSTAN_BIN", "mcp-phpstan-warm")
WORKING_DIR = os.environ.get("MCP_PHPSTAN_WORKING_DIR", os.getcwd())
SPAWN_TIMEOUT_SEC = 60
CALL_TIMEOUT_SEC = 180


SKIP_PATTERNS_ENV = "PHPSTAN_MCP_SKIP_PATTERNS"




PATHS_ENV = "PHPSTAN_MCP_PATHS"




import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent.parent / "presets" / "mcp"))
from _paths import socket_pid_paths as _shared_socket_pid_paths  
import _spawn  

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "common"))
import refusal as _refusal  
import ndjson_scan as _ndjson_scan  
from source_context import context_fields  
from spawnable import which_excluding_cwd  


def sock_paths(cwd: str, name: str) -> tuple[str, str]:
    return _shared_socket_pid_paths(cwd, name)


def resolve_bin(cwd: str) -> str:


    bin_path = DAEMON_PROC
    if not os.path.isabs(bin_path):
        if "/" in bin_path or os.sep in bin_path:


            candidate = os.path.abspath(os.path.join(cwd, bin_path))
            if not os.path.isfile(candidate):
                raise _refusal.DaemonUnavailable(
                    f"mcp-phpstan-warm not found at: {candidate}")
            bin_path = candidate
        else:
            resolved = which_excluding_cwd(bin_path)
            if resolved is None:
                raise _refusal.DaemonUnavailable(
                    "mcp-phpstan-warm not found on $PATH — install via: "
                    "composer require --dev dpt/mcp-phpstan-warm, or set "
                    "MCP_PHPSTAN_BIN (abs, or relative to the project root)."
                )
            bin_path = resolved
    return bin_path


def ensure_daemon(cwd: str) -> str:






    no_transport = _refusal.daemon_transport_reason()
    if no_transport:






        resolve_bin(cwd)
        raise _refusal.DaemonUnavailable(no_transport)
    try:
        return _spawn.ensure_daemon(
            cwd, DAEMON_NAME,
            preflight=lambda: resolve_bin(cwd),
            spawn_timeout=SPAWN_TIMEOUT_SEC,
        )
    except _spawn.AutospawnSuppressed:









        resolve_bin(cwd)
        raise


def ndjson_call(sock_path: str, file_path: str) -> dict:




    box = {"sock": sock_path}

    def attempt() -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(CALL_TIMEOUT_SEC)
            s.connect(box["sock"])












            req_id = random.randrange(2, 2**32)  
            init_id = random.randrange(2, 2**32)
            if init_id == req_id:
                init_id = init_id + 1 if init_id < 2**32 - 1 else init_id - 1
            msgs = [
                {"jsonrpc": "2.0", "id": init_id, "method": "initialize",
                 "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                            "clientInfo": {"name": "phpstan-mcp-adapter", "version": "1.0.0"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": req_id, "method": "tools/call",
                 "params": {"name": "phpstan_analyse",
                            "arguments": {"path": file_path}}},
            ]
            s.sendall(("\n".join(json.dumps(m) for m in msgs) + "\n").encode())







            return _ndjson_scan.receive_until(s, req_id, CALL_TIMEOUT_SEC, box["sock"],
                                               own_ids=frozenset((init_id,)))

    def respawn() -> None:
        box["sock"] = _spawn.force_respawn(
            WORKING_DIR, DAEMON_NAME, preflight=lambda: resolve_bin(WORKING_DIR),
            spawn_timeout=SPAWN_TIMEOUT_SEC)

    def pid_probe():






















        return _spawn.daemon_pid(_spawn.pid_path(box["sock"]))

    return _ndjson_scan.call_with_retry(attempt, respawn, pid_probe=pid_probe)


def is_refusal(msg: str) -> bool:
    return _refusal.is_refusal(msg, extra_patterns=os.environ.get("PHPSTAN_MCP_SKIP_PATTERNS", ""))


def skipped(file_path: str, reason: str, dur_ms: int) -> dict:
    return _refusal.skipped("phpstan-mcp", file_path, reason, dur_ms)


def format_response(file_path: str, mcp_resp: dict, dur_ms: int) -> dict:
    base = {"tool": "phpstan-mcp", "file": file_path,
            "ok": True, "count": 0, "errors": [], "duration_ms": dur_ms}

    if "error" in mcp_resp:
        base["ok"] = False
        base["count"] = 1
        base["errors"] = [{"line": None, "col": None, "severity": "error",
                           "code": "mcp", "msg": str(mcp_resp["error"])}]
        return base

    structured = (mcp_resp.get("result", {}) or {}).get("structuredContent") or {}
    errors = structured.get("errors") or []
    exit_code = structured.get("exit_code", 0)

    if errors:
        base["ok"] = False
        base["count"] = len(errors)
        for e in errors:
            line = e.get("line")
            try:
                line_int = int(line) if line else None
            except (TypeError, ValueError):
                line_int = None


            _msg = e.get("message", "")
            _cap = int(os.environ.get("PHPSTAN_MCP_MSG_MAX_CHARS", "2000"))
            if len(_msg) > _cap:
                _head = _cap - 80
                _msg = (_msg[:_head]
                        + f"... [TRUNCATED — {len(_msg) - _head} more chars; "
                        + "raise PHPSTAN_MCP_MSG_MAX_CHARS or run phpstan directly]")
            base["errors"].append({
                "line": line_int,
                "col": None,
                "severity": "error",
                "code": e.get("identifier") or "phpstan",
                "msg": _msg,
                **context_fields(file_path, line_int),
            })
    elif exit_code != 0:
        msg = structured.get("error") or f"phpstan exit {exit_code}"
        if is_refusal(msg):
            return skipped(file_path, msg, dur_ms)
        base["ok"] = False
        base["count"] = 1
        base["errors"] = [{"line": None, "col": None, "severity": "error",
                           "code": "phpstan.exit", "msg": msg}]
    return base


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        sys.stderr.write("usage: phpstan-mcp.py FILE\n")
        return 2
    file_path = argv[1]
    t0 = time.monotonic()
    out_of_scope = _refusal.outside_roots(file_path, PATHS_ENV, os.environ.get("PHPSTAN_MCP_PATHS", ""))
    if out_of_scope:
        print(json.dumps(skipped(file_path, out_of_scope,
                                 int((time.monotonic() - t0) * 1000))))
        return 0
    try:
        sock = ensure_daemon(WORKING_DIR)
        resp = ndjson_call(sock, os.path.abspath(file_path))
    except (_refusal.DaemonUnavailable, _spawn.AutospawnSuppressed) as e:












        print(json.dumps(_refusal.absent(
            "phpstan-mcp", file_path, str(e),
            int((time.monotonic() - t0) * 1000))))
        return 0
    dur_ms = int((time.monotonic() - t0) * 1000)
    print(json.dumps(format_response(file_path, resp, dur_ms)))
    return 0


if __name__ == "__main__":






    sys.exit(_refusal.guard_main("phpstan-mcp", main, sys.argv))
