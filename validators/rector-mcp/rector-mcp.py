#!/usr/bin/env python3










from __future__ import annotations

import functools
import hashlib
import json
import os
import random
import socket
import sys
import time
from pathlib import Path

DAEMON_NAME = os.environ.get("MCP_RECTOR_DAEMON_NAME", "rector-warm")
DAEMON_PROC = os.environ.get("MCP_RECTOR_BIN", "mcp-rector-warm")
WORKING_DIR = os.environ.get("MCP_RECTOR_WORKING_DIR", os.getcwd())
RECTOR_CONFIG = os.environ.get("MCP_RECTOR_CONFIG")  
SPAWN_TIMEOUT_SEC = 30
CALL_TIMEOUT_SEC = 120












_DEFAULT_ENGINE_GLITCHES = ("System error:", "toMutatingScope() on null")


def _supertool_config() -> dict:

    try:
        with open(os.path.join(WORKING_DIR, ".supertool.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


@functools.lru_cache(maxsize=1)
def engine_glitch_signatures() -> list[str]:




    sigs = (((_supertool_config().get("validators") or {}).get("rector") or {})
            .get("engine_glitches"))
    if isinstance(sigs, list):
        return [str(s).strip() for s in sigs if str(s).strip()]
    return list(_DEFAULT_ENGINE_GLITCHES)


def is_engine_glitch(msg: str) -> bool:

    return bool(msg) and any(sig in msg for sig in engine_glitch_signatures())




import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent.parent / "presets" / "mcp"))
from _paths import socket_pid_paths as _shared_socket_pid_paths  
import _spawn  

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "common"))
import refusal as _refusal  
import ndjson_scan as _ndjson_scan  


def sock_paths(cwd: str, name: str) -> tuple[str, str]:
    return _shared_socket_pid_paths(cwd, name)


def resolve_bin(cwd: str) -> str:




    bin_path = DAEMON_PROC
    if not os.path.isabs(bin_path):
        if "/" in bin_path or os.sep in bin_path:


            candidate = os.path.abspath(os.path.join(cwd, bin_path))
            if not os.path.isfile(candidate):
                raise _refusal.DaemonUnavailable(
                    f"mcp-rector-warm not found at: {candidate}")
            bin_path = candidate
        else:
            from spawnable import which_excluding_cwd
            resolved = which_excluding_cwd(bin_path)
            if resolved is None:
                raise _refusal.DaemonUnavailable(
                    "mcp-rector-warm not found on $PATH — install via: "
                    "composer global require dpt/mcp-rector-warm, or set "
                    "MCP_RECTOR_BIN (abs, or relative to the project root)."
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
                            "clientInfo": {"name": "rector-mcp-adapter", "version": "1.0.0"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": req_id, "method": "tools/call",
                 "params": {"name": "rector_process",
                            "arguments": {"path": file_path, "dryRun": True}}},
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

    base = {"tool": "rector-mcp", "file": file_path,
            "ok": True, "count": 0, "errors": [], "duration_ms": duration_ms}

    if "error" in mcp_resp:
        base["ok"] = False
        base["count"] = 1
        base["errors"] = [{"line": None, "col": None, "severity": "error",
                           "code": "mcp", "msg": str(mcp_resp["error"])}]
        return base

    result = mcp_resp.get("result", {})
    structured = result.get("structuredContent") or {}
    exit_code = structured.get("exit_code", 0)
    output = structured.get("output", "")



    rector_json = None
    text = output or ""
    brace = text.find("{")
    if brace != -1:
        try:
            rector_json, _ = json.JSONDecoder().raw_decode(text[brace:])
        except json.JSONDecodeError:
            rector_json = None

    if rector_json:
        file_diffs = rector_json.get("file_diffs", []) or []
        errors = rector_json.get("errors", []) or []





        for fd in file_diffs:
            applied = fd.get("applied_rectors") or []
            diff = fd.get("diff") or ""
            if not applied and not diff:
                continue
            base["ok"] = False
            base["count"] += 1
            rules = [r.rsplit("\\", 1)[-1] for r in applied]
            rules_str = ", ".join(rules) if rules else "unknown rule"
            base["errors"].append({
                "line": None, "col": None, "severity": "warning",
                "code": "rector.refactor",
                "msg": f"Would apply {rules_str}",
                "diff": diff,
            })
        if errors:
            for e in errors:
                msg = e.get("message", str(e)) if isinstance(e, dict) else str(e)









                if is_engine_glitch(msg):
                    continue
                base["ok"] = False
                base["count"] += 1


                _cap = int(os.environ.get("RECTOR_MCP_MSG_MAX_CHARS", "2000"))
                if len(msg) > _cap:
                    head = _cap - 80
                    msg = (msg[:head]
                           + f"... [TRUNCATED — {len(msg) - head} more chars; "
                           + "raise RECTOR_MCP_MSG_MAX_CHARS or run rector directly]")
                base["errors"].append({"line": None, "col": None, "severity": "error",
                                       "code": "rector.error", "msg": msg})
    elif exit_code != 0:
        base["ok"] = False
        base["count"] = 1
        base["errors"] = [{"line": None, "col": None, "severity": "error",
                           "code": "rector.exit",
                           "msg": f"rector exit {exit_code}"}]

    return base


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        sys.stderr.write("usage: rector-mcp.py FILE\n")
        return 2
    file_path = argv[1]
    t0 = time.monotonic()
    try:
        sock = ensure_daemon(WORKING_DIR)
        resp = ndjson_call(sock, os.path.abspath(file_path))
    except (_refusal.DaemonUnavailable, _spawn.AutospawnSuppressed) as e:












        print(json.dumps(_refusal.absent(
            "rector-mcp", file_path, str(e),
            int((time.monotonic() - t0) * 1000))))
        return 0
    duration_ms = int((time.monotonic() - t0) * 1000)
    print(json.dumps(format_response(file_path, resp, duration_ms)))
    return 0


if __name__ == "__main__":






    sys.exit(_refusal.guard_main("rector-mcp", main, sys.argv))
