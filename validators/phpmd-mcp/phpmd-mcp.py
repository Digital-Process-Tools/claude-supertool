#!/usr/bin/env python3
"""PHPMD validator via warm MCP daemon.

Usage: phpmd-mcp.py FILE

Connects to the long-lived mcp-phpmd-warm daemon over UDS. Auto-spawns on first call.
Daemon name + working dir + rulesets are read from $MCP_PHPMD_* env vars (set by the
`cmd` template in .supertool.json), with sensible fallbacks.

Output: SCHEMA.md-compliant JSON on stdout (single line). PHPMD findings are emitted as
`severity: "warning"` — this validator is non-blocking by design (rollback_on_fail: false),
so smells surface at edit time without reverting the edit.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time

# Reuse the shared 5-line source-context helper (same one the cold phpmd adapter uses).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "common"))
from source_context import source_context  # noqa: E402

DAEMON_NAME = os.environ.get("MCP_PHPMD_DAEMON_NAME", "phpmd-warm")
DAEMON_PROC = os.environ.get("MCP_PHPMD_BIN", "mcp-phpmd-warm")
WORKING_DIR = os.environ.get("MCP_PHPMD_WORKING_DIR", os.getcwd())
SPAWN_TIMEOUT_SEC = 30
CALL_TIMEOUT_SEC = 120

# #148: use the shared presets/mcp/_paths helper so client + daemon agree on the
# runtime dir ($XDG_RUNTIME_DIR/supertool/mcp/ etc.).
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "presets", "mcp",
))
from _paths import socket_pid_paths as _shared_socket_pid_paths  # noqa: E402
import _spawn  # noqa: E402  (#451: one daemon per (kind, config fingerprint))

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "common"))
import refusal as _refusal  # noqa: E402

SKIP_PATTERNS_ENV = "PHPMD_MCP_SKIP_PATTERNS"


def sock_paths(cwd: str, name: str) -> tuple[str, str]:
    return _shared_socket_pid_paths(cwd, name)


def resolve_bin(cwd: str) -> str:
    """Resolve the mcp-phpmd-warm binary.

    Abs path used as-is. A relative path WITH a separator (e.g.
    "Dvsi/dvsi-private/libs/bin/mcp-phpmd-warm") is resolved against cwd (the
    project root) — this keeps a committed/shared .supertool.json portable
    across machines. A bare name falls back to $PATH lookup. Spawn-path only.
    """
    bin_path = DAEMON_PROC
    if not os.path.isabs(bin_path):
        if "/" in bin_path or os.sep in bin_path:
            candidate = os.path.abspath(os.path.join(cwd, bin_path))
            if not os.path.isfile(candidate):
                raise _refusal.DaemonUnavailable(
                    f"mcp-phpmd-warm not found at: {candidate}")
            bin_path = candidate
        else:
            from shutil import which
            resolved = which(bin_path)
            if resolved is None:
                raise _refusal.DaemonUnavailable(
                    "mcp-phpmd-warm not found on $PATH — install via: "
                    "composer global require dpt/mcp-phpmd-warm, or set "
                    "MCP_PHPMD_BIN (abs, or relative to the project root)."
                )
            bin_path = resolved
    return bin_path


def ensure_daemon(cwd: str) -> str:
    """The socket of *the* warm phpmd daemon — started, reused, or replaced.

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
    """Initialize + tools/call(phpmd_analyse). Returns parsed MCP response dict."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(CALL_TIMEOUT_SEC)
        s.connect(sock_path)

        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "phpmd-mcp-adapter", "version": "1.0.0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "phpmd_analyse",
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


def format_response(file_path: str, mcp_resp: dict, duration_ms: int) -> dict:
    """Convert MCP response to SCHEMA.md validator JSON. PHPMD violations → warnings."""
    base = {"tool": "phpmd-mcp", "file": file_path,
            "ok": True, "count": 0, "errors": [], "duration_ms": duration_ms}

    if "error" in mcp_resp:
        base["ok"] = False
        base["count"] = 1
        base["errors"] = [{"line": None, "col": None, "severity": "error",
                           "code": "mcp", "msg": str(mcp_resp["error"])}]
        return base

    structured = (mcp_resp.get("result", {}) or {}).get("structuredContent") or {}

    # The tool returns a SecurityError / runtime error as an extra key.
    if structured.get("error"):
        # A scope refusal is not a runtime error — it is an absence of analysis,
        # and counting it as one error inflates the delta by +1 (#406).
        if _refusal.is_refusal(str(structured["error"]), SKIP_PATTERNS_ENV):
            return _refusal.skipped("phpmd-mcp", file_path,
                                    str(structured["error"]), duration_ms)
        base["ok"] = False
        base["count"] = 1
        base["errors"] = [{"line": None, "col": None, "severity": "error",
                           "code": structured.get("error_class", "phpmd.error"),
                           "msg": str(structured["error"])}]
        return base

    output = structured.get("output", "") or ""
    try:
        report = json.loads(output) if output else {}
    except json.JSONDecodeError:
        base["ok"] = False
        base["count"] = 1
        base["errors"] = [{"line": None, "col": None, "severity": "error",
                           "code": "phpmd.parse", "msg": "could not parse PHPMD JSON output"}]
        return base

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
                "source_context": source_context(file_path, line) if line else None,
            })

    # PHPMD-level processing errors (parse failures etc.).
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
        _refusal.require_daemon_transport()
        sock = ensure_daemon(WORKING_DIR)
        resp = ndjson_call(sock, os.path.abspath(file_path))
    except _refusal.DaemonUnavailable as e:
        # Not installed for this working directory — every `cwd:` into a git
        # worktree lands here. Nothing was analysed, so nothing is reported.
        print(json.dumps(_refusal.skipped(
            "phpmd-mcp", file_path, str(e),
            int((time.monotonic() - t0) * 1000))))
        return 0
    except Exception as e:
        import traceback
        tb = traceback.format_exc().splitlines()[-3:]
        print(json.dumps({
            "tool": "phpmd-mcp", "file": file_path, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": f"{type(e).__name__}: {e} | trace: {' | '.join(tb)}"}],
            "duration_ms": int((time.monotonic() - t0) * 1000),
        }))
        return 0
    duration_ms = int((time.monotonic() - t0) * 1000)
    print(json.dumps(format_response(file_path, resp, duration_ms)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
