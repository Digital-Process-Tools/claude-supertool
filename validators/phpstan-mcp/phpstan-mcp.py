#!/usr/bin/env python3
"""PHPStan validator via warm MCP daemon.

Usage: phpstan-mcp.py FILE

Connects to long-lived mcp-phpstan-warm daemon over UDS. Auto-spawns on first call.
Daemon spawns a phpstan worker (TCP NDJSON protocol) that stays warm between calls.
Env vars: MCP_PHPSTAN_* set by `cmd` template in .supertool.json.

Output: SCHEMA.md-compliant JSON on stdout.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import socket
import sys
import time
from shutil import which

DAEMON_NAME = os.environ.get("MCP_PHPSTAN_DAEMON_NAME", "phpstan-warm")
DAEMON_PROC = os.environ.get("MCP_PHPSTAN_BIN", "mcp-phpstan-warm")
WORKING_DIR = os.environ.get("MCP_PHPSTAN_WORKING_DIR", os.getcwd())
SPAWN_TIMEOUT_SEC = 60
CALL_TIMEOUT_SEC = 180

# Extra refusal substrings (comma-separated), opt-in per repo.
SKIP_PATTERNS_ENV = "PHPSTAN_MCP_SKIP_PATTERNS"

# Analysis roots, opt-in per repo (#412). Set, a target outside every root is
# skipped here instead of costing a ~9s daemon round trip to be told the same.
# Unset, the daemon stays the only authority on scope — see refusal.outside_roots.
PATHS_ENV = "PHPSTAN_MCP_PATHS"


# #148: use the shared presets/mcp/_paths helper so client + daemon agree on
# the runtime dir (was /tmp/, now $XDG_RUNTIME_DIR/supertool/mcp/ etc.).
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent.parent / "presets" / "mcp"))
from _paths import socket_pid_paths as _shared_socket_pid_paths  # noqa: E402
import _spawn  # noqa: E402  (#451: one daemon per (kind, config fingerprint))

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "common"))
import refusal as _refusal  # noqa: E402


def sock_paths(cwd: str, name: str) -> tuple[str, str]:
    return _shared_socket_pid_paths(cwd, name)


def resolve_bin(cwd: str) -> str:
    """Locate the MCP server binary. Spawn-path only — a good error beats a
    daemon that starts and immediately dies."""
    bin_path = DAEMON_PROC
    if not os.path.isabs(bin_path):
        if "/" in bin_path or os.sep in bin_path:
            # Relative path with a separator → resolve against cwd (project root).
            # Keeps a committed/shared .supertool.json portable across machines.
            candidate = os.path.abspath(os.path.join(cwd, bin_path))
            if not os.path.isfile(candidate):
                raise _refusal.DaemonUnavailable(
                    f"mcp-phpstan-warm not found at: {candidate}")
            bin_path = candidate
        else:
            resolved = which(bin_path)
            if resolved is None:
                raise _refusal.DaemonUnavailable(
                    "mcp-phpstan-warm not found on $PATH — install via: "
                    "composer require --dev dpt/mcp-phpstan-warm, or set "
                    "MCP_PHPSTAN_BIN (abs, or relative to the project root)."
                )
            bin_path = resolved
    return bin_path


def ensure_daemon(cwd: str) -> str:
    """The socket of *the* warm phpstan daemon — started, reused, or replaced.

    Delegates to presets/mcp/_spawn (#451): the check-and-spawn runs under an
    exclusive lock, and a daemon holding a config that no longer matches disk
    is retired rather than asked for an answer.
    """
    return _spawn.ensure_daemon(
        cwd, DAEMON_NAME,
        preflight=lambda: resolve_bin(cwd),
        spawn_timeout=SPAWN_TIMEOUT_SEC,
    )


def ndjson_call(sock_path: str, file_path: str) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(CALL_TIMEOUT_SEC)
        s.connect(sock_path)
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "phpstan-mcp-adapter", "version": "1.0.0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "phpstan_analyse",
                        "arguments": {"path": file_path}}},
        ]
        s.sendall(("\n".join(json.dumps(m) for m in msgs) + "\n").encode())
        buf = b""
        deadline = time.monotonic() + CALL_TIMEOUT_SEC
        while time.monotonic() < deadline:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            for line in buf.splitlines():
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and obj.get("id") == 2:
                    return obj
        raise RuntimeError("no id=2 response received within timeout")


def source_context(file_path: str, error_line: int | None) -> list[str]:
    if error_line is None or error_line <= 0:
        return []
    try:
        lines = pathlib.Path(file_path).read_text(errors="replace", encoding="utf-8").splitlines()
    except OSError:
        return []
    ctx = []
    for offset in range(-2, 3):
        ln = error_line + offset
        if 1 <= ln <= len(lines):
            prefix = f"{ln}\u2192" if offset == 0 else f"{ln}:"
            ctx.append(f"{prefix} {lines[ln - 1]}")
    return ctx


def is_refusal(msg: str) -> bool:
    return _refusal.is_refusal(msg, SKIP_PATTERNS_ENV)


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
            # Cap msg — phpstan can dump multi-MB type-info dumps that
            # explode validator output. Override via PHPSTAN_MCP_MSG_MAX_CHARS.
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
                "source_context": source_context(file_path, line_int),
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
    out_of_scope = _refusal.outside_roots(file_path, PATHS_ENV)
    if out_of_scope:
        print(json.dumps(skipped(file_path, out_of_scope,
                                 int((time.monotonic() - t0) * 1000))))
        return 0
    try:
        _refusal.require_daemon_transport()
        sock = ensure_daemon(WORKING_DIR)
        resp = ndjson_call(sock, os.path.abspath(file_path))
    except _refusal.DaemonUnavailable as e:
        # Not installed for this working directory — every `cwd:` into a git
        # worktree lands here. Nothing was analysed, so nothing is reported.
        print(json.dumps(skipped(file_path, str(e),
                                 int((time.monotonic() - t0) * 1000))))
        return 0
    except Exception as e:
        import traceback
        tb = traceback.format_exc().splitlines()[-3:]
        print(json.dumps({
            "tool": "phpstan-mcp", "file": file_path, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": f"{type(e).__name__}: {e} | trace: {' | '.join(tb)}"}],
            "duration_ms": int((time.monotonic() - t0) * 1000),
        }))
        return 0
    dur_ms = int((time.monotonic() - t0) * 1000)
    print(json.dumps(format_response(file_path, resp, dur_ms)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
