#!/usr/bin/env python3










from __future__ import annotations

import hashlib
import json
import os
import random
import socket
import sys
import time

DAEMON_NAME = os.environ.get("MCP_PHPUNIT_DAEMON_NAME", "phpunit-warm")
DAEMON_PROC = os.environ.get("MCP_PHPUNIT_BIN", "mcp-phpunit-warm")
WORKING_DIR = os.environ.get("MCP_PHPUNIT_WORKING_DIR", os.getcwd())
SPAWN_TIMEOUT_SEC = 30
CALL_TIMEOUT_SEC = 300



MSG_MAX_CHARS = int(os.environ.get("PHPUNIT_MCP_MSG_MAX_CHARS", "2000"))


def _cap_msg(msg: str) -> str:

    if len(msg) <= MSG_MAX_CHARS:
        return msg
    head = MSG_MAX_CHARS - 80
    return (
        msg[:head]
        + f"... [TRUNCATED — {len(msg) - head} more chars; "
        + "raise PHPUNIT_MCP_MSG_MAX_CHARS or run phpunit directly to see full]"
    )




import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent.parent / "presets" / "mcp"))
from _paths import socket_pid_paths as _shared_socket_pid_paths  
import _spawn  

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "common"))
import refusal as _refusal  
import ndjson_scan as _ndjson_scan  
from spawnable import which_excluding_cwd  
from source_context import context_fields  


def sock_paths(cwd: str, name: str) -> tuple[str, str]:
    return _shared_socket_pid_paths(cwd, name)


def resolve_bin(cwd: str) -> str:


    bin_path = DAEMON_PROC
    if not os.path.isabs(bin_path):
        if "/" in bin_path or os.sep in bin_path:


            candidate = os.path.abspath(os.path.join(cwd, bin_path))
            if not os.path.isfile(candidate):
                raise _refusal.DaemonUnavailable(
                    f"mcp-phpunit-warm not found at: {candidate}")
            bin_path = candidate
        else:
            resolved = which_excluding_cwd(bin_path)
            if resolved is None:
                raise _refusal.DaemonUnavailable(
                    "mcp-phpunit-warm not found on $PATH — install via: "
                    "composer global require dpt/mcp-phpunit-warm, or set "
                    "MCP_PHPUNIT_BIN (abs, or relative to the project root)."
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


def ndjson_call(sock_path: str, test_file: str) -> dict:




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
                            "clientInfo": {"name": "phpunit-mcp-adapter", "version": "1.0.0"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": req_id, "method": "tools/call",
                 "params": {"name": "phpunit_run",
                            "arguments": {"testFile": test_file}}},
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


def parse_json_output(file_path: str, output_json: str, dur_ms: int) -> dict:
    base = {"tool": "phpunit-mcp", "file": file_path, "ok": True, "count": 0,
            "errors": [], "duration_ms": dur_ms}
    try:
        data = json.loads(output_json)
    except json.JSONDecodeError as e:
        base["ok"] = False
        base["count"] = 1
        base["errors"] = [{"line": None, "col": None, "severity": "error",
                           "code": "adapter", "msg": f"output parse: {e}"}]
        return base

    failures = data.get("failures", [])
    errors   = data.get("errors", [])
    skipped  = data.get("skipped", [])

    err_list = []
    for entry in failures:
        line_int = entry.get("line") or None
        err_list.append({
            "line": line_int,
            "col": None,
            "severity": "error",
            "code": "phpunit.failure",
            "msg": _cap_msg(f"{entry.get('method', '?')}: {entry.get('message', '')}"),
            **context_fields(entry.get("file", file_path), line_int),
        })
    for entry in errors:
        line_int = entry.get("line") or None
        err_list.append({
            "line": line_int,
            "col": None,
            "severity": "error",
            "code": "phpunit.error",
            "msg": _cap_msg(f"{entry.get('method', '?')}: {entry.get('message', '')}"),
            **context_fields(entry.get("file", file_path), line_int),
        })

    tests_total = data.get("tests", 0)
    fail_total  = len(failures) + len(errors)
    assertions  = data.get("assertions", 0)
    skipped_n   = len(skipped)

    base["ok"]     = fail_total == 0
    base["count"]  = fail_total
    base["errors"] = err_list
    base["metrics"] = {
        "tests_total":   tests_total,
        "tests_passed":  tests_total - fail_total - skipped_n,
        "tests_skipped": skipped_n,
        "assertions":    assertions,
    }
    return base


def format_response(file_path: str, mcp_resp: dict, dur_ms: int) -> dict:
    base = {"tool": "phpunit-mcp", "file": file_path,
            "ok": True, "count": 0, "errors": [], "duration_ms": dur_ms}

    if "error" in mcp_resp:
        base["ok"] = False
        base["count"] = 1
        base["errors"] = [{"line": None, "col": None, "severity": "error",
                           "code": "mcp", "msg": str(mcp_resp["error"])}]
        return base

    structured = (mcp_resp.get("result", {}) or {}).get("structuredContent") or {}
    output    = structured.get("output", "") or ""
    exit_code = structured.get("exit_code", 0)


    if output.strip().startswith("{"):
        return parse_json_output(file_path, output, dur_ms)


    if exit_code != 0:
        base["ok"] = False
        base["count"] = 1
        base["errors"] = [{"line": None, "col": None, "severity": "error",
                           "code": "phpunit.exit",
                           "msg": f"phpunit exit {exit_code} with no output"}]
    return base


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        sys.stderr.write("usage: phpunit-mcp.py FILE\n")
        return 2
    file_path = argv[1]
    t0 = time.monotonic()
    try:
        sock = ensure_daemon(WORKING_DIR)
        resp = ndjson_call(sock, os.path.abspath(file_path))
    except (_refusal.DaemonUnavailable, _spawn.AutospawnSuppressed) as e:












        print(json.dumps(_refusal.absent(
            "phpunit-mcp", file_path, str(e),
            int((time.monotonic() - t0) * 1000))))
        return 0
    dur_ms = int((time.monotonic() - t0) * 1000)
    print(json.dumps(format_response(file_path, resp, dur_ms)))
    return 0


if __name__ == "__main__":






    sys.exit(_refusal.guard_main("phpunit-mcp", main, sys.argv))
