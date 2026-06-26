#!/usr/bin/env python3
"""Rector validator via warm MCP daemon.

Usage: rector-mcp.py FILE

Connects to the long-lived mcp-rector-warm daemon over UDS. Auto-spawns on first call.
Daemon name + working dir + rector config are read from $MCP_RECTOR_* env vars (set by
the `cmd` template in .supertool.json), with sensible fallbacks.

Output: SCHEMA.md-compliant JSON on stdout (single line).
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

DAEMON_NAME = os.environ.get("MCP_RECTOR_DAEMON_NAME", "rector-warm")
DAEMON_PROC = os.environ.get("MCP_RECTOR_BIN", "mcp-rector-warm")
WORKING_DIR = os.environ.get("MCP_RECTOR_WORKING_DIR", os.getcwd())
RECTOR_CONFIG = os.environ.get("MCP_RECTOR_CONFIG")  # optional
SPAWN_TIMEOUT_SEC = 30
CALL_TIMEOUT_SEC = 120

# Non-deterministic warm-daemon engine glitches: PHP fatals / engine-state
# corruption inside rector itself, NOT findings about the edited file (a cold
# `rector` CLI handles the same file clean). Signatures are a config prop in
# .supertool.json: validators.rector.engine_glitches (a JSON list of substrings).
# The signature *values* are config, not env. The file is located in WORKING_DIR
# (the project root supertool runs from; pinnable via MCP_RECTOR_WORKING_DIR) —
# a single read, no parent-dir walk (unlike daemon.py / the core loader), which is
# fine because the daemon cmd runs at the project root where .supertool.json lives.
# The generic supertool core stays oblivious to these signatures. Built-in defaults
# below are the safety net when the prop is absent or the file can't be read.
# Substring match, case-sensitive. Add new signatures in .supertool.json, no code change.
_DEFAULT_ENGINE_GLITCHES = ("System error:", "toMutatingScope() on null")


def _supertool_config() -> dict:
    """Load .supertool.json from the working dir (project root). {} on any failure."""
    try:
        with open(os.path.join(WORKING_DIR, ".supertool.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


@functools.lru_cache(maxsize=1)
def engine_glitch_signatures() -> list[str]:
    """Glitch signatures from .supertool.json validators.rector.engine_glitches,
    or the built-in defaults when the prop is absent / not a list / unreadable.
    Memoized: the adapter is a fresh process per validator call, so the config
    can't change underneath a run — this avoids re-reading the file per error."""
    sigs = (((_supertool_config().get("validators") or {}).get("rector") or {})
            .get("engine_glitches"))
    if isinstance(sigs, list):
        return [str(s).strip() for s in sigs if str(s).strip()]
    return list(_DEFAULT_ENGINE_GLITCHES)


def is_engine_glitch(msg: str) -> bool:
    """True if `msg` matches a configured non-deterministic engine glitch."""
    return bool(msg) and any(sig in msg for sig in engine_glitch_signatures())


# #148: use the shared presets/mcp/_paths helper so client + daemon agree on
# the runtime dir (was /tmp/, now $XDG_RUNTIME_DIR/supertool/mcp/ etc.).
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent.parent / "presets" / "mcp"))
from _paths import socket_pid_paths as _shared_socket_pid_paths  # noqa: E402


def sock_paths(cwd: str, name: str) -> tuple[str, str]:
    return _shared_socket_pid_paths(cwd, name)


def ensure_daemon(cwd: str) -> str:
    sock, pid = sock_paths(cwd, DAEMON_NAME)
    if os.path.exists(sock) and is_alive(pid):
        return sock

    # Resolve mcp-rector-warm binary: env override > $PATH > absolute path in env.
    bin_path = DAEMON_PROC
    if not os.path.isabs(bin_path):
        if "/" in bin_path or os.sep in bin_path:
            # Relative path with a separator → resolve against cwd (project root).
            # Keeps a committed/shared .supertool.json portable across machines.
            candidate = os.path.abspath(os.path.join(cwd, bin_path))
            if not os.path.isfile(candidate):
                raise RuntimeError(f"mcp-rector-warm not found at: {candidate}")
            bin_path = candidate
        else:
            from shutil import which
            resolved = which(bin_path)
            if resolved is None:
                raise RuntimeError(
                    f"mcp-rector-warm not found on $PATH. Install via: composer global require dpt/mcp-rector-warm\n"
                    f"Or set MCP_RECTOR_BIN to a path (abs, or relative to the project root)."
                )
            bin_path = resolved

    # daemon.py lives in supertool's presets/mcp/. Find it relative to this adapter file.
    # adapter = .../claude-supertool/validators/rector-mcp/rector-mcp.py → 3 levels up = supertool root.
    supertool_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    daemon_script = os.path.join(supertool_root, "presets/mcp/daemon.py")
    if not os.path.isfile(daemon_script):
        raise RuntimeError(f"daemon.py not found: {daemon_script}")

    # Write spec into a temp .supertool.json override? Simpler: assume caller's
    # .supertool.json has mcp.<DAEMON_NAME> entry. daemon.py reads it from cwd.
    proc = subprocess.Popen(
        ["python3", daemon_script, DAEMON_NAME, "--detach"],
        cwd=cwd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proc.wait(timeout=5)

    deadline = time.monotonic() + SPAWN_TIMEOUT_SEC
    while time.monotonic() < deadline:
        if os.path.exists(sock):
            return sock
        time.sleep(0.1)
    raise RuntimeError(f"daemon failed to bind {sock} within {SPAWN_TIMEOUT_SEC}s")


def is_alive(pid_path: str) -> bool:
    try:
        with open(pid_path) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def ndjson_call(sock_path: str, file_path: str) -> dict:
    """Initialize + tools/call(rector_process). Returns parsed MCP response dict."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(CALL_TIMEOUT_SEC)
        s.connect(sock_path)

        # initialize + notify + call — daemon bridges raw stdio so we speak JSON-RPC.
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "rector-mcp-adapter", "version": "1.0.0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "rector_process",
                        "arguments": {"path": file_path, "dryRun": True}}},
        ]
        s.sendall(("\n".join(json.dumps(m) for m in msgs) + "\n").encode())

        # Read until id=2 response or EOF.
        buf = b""
        deadline = time.monotonic() + CALL_TIMEOUT_SEC
        while time.monotonic() < deadline:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            # Try parse: look for response with id=2.
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
    """Convert MCP response to SCHEMA.md validator JSON."""
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

    # Parse rector's JSON output if present. JsonOutputFormatter emits pretty-printed
    # multi-line JSON, so find the first '{' and use raw_decode on the rest.
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

        # Surface refactor suggestions ONLY when we have actionable detail (applied_rectors
        # or a diff). Bare "would refactor X" with no specifics is noise — rector returns it
        # in --debug mode (which we need for speed) but it has no signal. Real errors below
        # always pass through.
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
                # Drop non-deterministic engine glitches at the source: a PHP fatal
                # or stale-reflection error from inside rector's warm daemon is not a
                # finding about this file (a cold `rector` CLI handles it clean), so it
                # must never surface as a red or get cached. Signatures are configured
                # per-mcp via the .supertool.json validators.rector.engine_glitches prop; see
                # _DEFAULT_ENGINE_GLITCHES. Root cause for the original "System error:
                # ClassReflection" case is fixed upstream in mcp-rector-warm 0.4.0
                # (claude-supertool#273); this stays to absorb future engine glitches
                # (e.g. "toMutatingScope() on null", #345).
                if is_engine_glitch(msg):
                    continue
                base["ok"] = False
                base["count"] += 1
                # Cap msg — rector can dump diffs that explode validator output.
                # Override via env: RECTOR_MCP_MSG_MAX_CHARS.
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
    except Exception as e:
        import traceback
        tb = traceback.format_exc().splitlines()[-3:]
        print(json.dumps({
            "tool": "rector-mcp", "file": file_path, "ok": False, "count": 1,
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
