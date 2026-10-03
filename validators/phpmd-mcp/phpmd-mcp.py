#!/usr/bin/env python3












from __future__ import annotations

import json
import os
import random
import socket
import sys
import time


sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "common"))
from source_context import context_fields  

DAEMON_NAME = os.environ.get("MCP_PHPMD_DAEMON_NAME", "phpmd-warm")
DAEMON_PROC = os.environ.get("MCP_PHPMD_BIN", "mcp-phpmd-warm")
WORKING_DIR = os.environ.get("MCP_PHPMD_WORKING_DIR", os.getcwd())
SPAWN_TIMEOUT_SEC = 30
CALL_TIMEOUT_SEC = 120



sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "presets", "mcp",
))
from _paths import socket_pid_paths as _shared_socket_pid_paths  
import _spawn  

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "common"))
import refusal as _refusal  
import ndjson_scan as _ndjson_scan  

SKIP_PATTERNS_ENV = "PHPMD_MCP_SKIP_PATTERNS"


def sock_paths(cwd: str, name: str) -> tuple[str, str]:
    return _shared_socket_pid_paths(cwd, name)


def resolve_bin(cwd: str) -> str:







    bin_path = DAEMON_PROC
    if not os.path.isabs(bin_path):
        if "/" in bin_path or os.sep in bin_path:
            candidate = os.path.abspath(os.path.join(cwd, bin_path))
            if not os.path.isfile(candidate):
                raise _refusal.DaemonUnavailable(
                    f"mcp-phpmd-warm not found at: {candidate}")
            bin_path = candidate
        else:
            from spawnable import which_excluding_cwd
            resolved = which_excluding_cwd(bin_path)
            if resolved is None:
                raise _refusal.DaemonUnavailable(
                    "mcp-phpmd-warm not found on $PATH — install via: "
                    "composer global require dpt/mcp-phpmd-warm, or set "
                    "MCP_PHPMD_BIN (abs, or relative to the project root)."
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
                            "clientInfo": {"name": "phpmd-mcp-adapter", "version": "1.0.0"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": req_id, "method": "tools/call",
                 "params": {"name": "phpmd_analyse",
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


def format_response(file_path: str, mcp_resp: dict, duration_ms: int) -> dict:

    base = {"tool": "phpmd-mcp", "file": file_path,
            "ok": True, "count": 0, "errors": [], "duration_ms": duration_ms}

    if "error" in mcp_resp:
        base["ok"] = False
        base["count"] = 1
        base["errors"] = [{"line": None, "col": None, "severity": "error",
                           "code": "mcp", "msg": str(mcp_resp["error"])}]
        return base

    structured = (mcp_resp.get("result", {}) or {}).get("structuredContent") or {}

    output = structured.get("output", "") or ""
    unreadable = False
    try:
        report = json.loads(output) if output else {}
    except json.JSONDecodeError:




        report = {}
        unreadable = True
    if unreadable and not structured.get("error"):
        base["ok"] = False
        base["count"] = 1
        base["errors"] = [{"line": None, "col": None, "severity": "error",
                           "code": "phpmd.parse", "msg": "could not parse PHPMD JSON output"}]
        return base







    if structured.get("error"):





        has_report = bool(report.get("errors")) or any(
            (f or {}).get("violations") for f in (report.get("files", []) or []))







        if _refusal.is_refusal(str(structured["error"]), SKIP_PATTERNS_ENV):
            if not has_report:
                return _refusal.skipped("phpmd-mcp", file_path,
                                        str(structured["error"]), duration_ms)
        else:



            base["ok"] = False
            base["count"] = 1
            base["errors"] = [{"line": None, "col": None, "severity": "error",
                               "code": structured.get("error_class", "phpmd.error"),
                               "msg": str(structured["error"])}]

    for file_entry in report.get("files", []) or []:
        for v in file_entry.get("violations", []) or []:
            line = v.get("beginLine")
            base["ok"] = False
            base["count"] += 1
            base["errors"].append({
                "line": line,
                "col": None,
                "severity": "warning",
                "code": v.get("rule"),
                "msg": (v.get("description") or "").strip(),
                **context_fields(file_path, line),
            })


    for e in report.get("errors", []) or []:
        base["ok"] = False
        base["count"] += 1
        base["errors"].append({
            "line": None, "col": None, "severity": "error",
            "code": "phpmd.error",
            "msg": (e.get("message") if isinstance(e, dict) else str(e)) or "phpmd error",
        })

    return base


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        sys.stderr.write("usage: phpmd-mcp.py FILE\n")
        return 2
    file_path = argv[1]
    t0 = time.monotonic()
    try:
        sock = ensure_daemon(WORKING_DIR)
        resp = ndjson_call(sock, os.path.abspath(file_path))
    except (_refusal.DaemonUnavailable, _spawn.AutospawnSuppressed) as e:












        print(json.dumps(_refusal.absent(
            "phpmd-mcp", file_path, str(e),
            int((time.monotonic() - t0) * 1000))))
        return 0
    duration_ms = int((time.monotonic() - t0) * 1000)
    print(json.dumps(format_response(file_path, resp, duration_ms)))
    return 0


if __name__ == "__main__":






    sys.exit(_refusal.guard_main("phpmd-mcp", main, sys.argv))
