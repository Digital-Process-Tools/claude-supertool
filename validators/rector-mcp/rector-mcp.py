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
import random
import socket
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
        with open(os.path.join(WORKING_DIR, ".supertool.json"), encoding="utf-8") as f:
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
import _spawn  # noqa: E402  (#451: one daemon per (kind, config fingerprint))

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "common"))
import refusal as _refusal  # noqa: E402
import ndjson_scan as _ndjson_scan  # noqa: E402  (#1924: a response glued to noise)


def sock_paths(cwd: str, name: str) -> tuple[str, str]:
    return _shared_socket_pid_paths(cwd, name)


def resolve_bin(cwd: str) -> str:
    """Resolve mcp-rector-warm: env override > $PATH > absolute path in env.

    Spawn-path only — a good error beats a daemon that starts and dies.
    """
    bin_path = DAEMON_PROC
    if not os.path.isabs(bin_path):
        if "/" in bin_path or os.sep in bin_path:
            # Relative path with a separator → resolve against cwd (project root).
            # Keeps a committed/shared .supertool.json portable across machines.
            candidate = os.path.abspath(os.path.join(cwd, bin_path))
            if not os.path.isfile(candidate):
                raise _refusal.DaemonUnavailable(
                    f"mcp-rector-warm not found at: {candidate}")
            bin_path = candidate
        else:
            from shutil import which
            resolved = which(bin_path)
            if resolved is None:
                raise _refusal.DaemonUnavailable(
                    "mcp-rector-warm not found on $PATH — install via: "
                    "composer global require dpt/mcp-rector-warm, or set "
                    "MCP_RECTOR_BIN (abs, or relative to the project root)."
                )
            bin_path = resolved
    return bin_path


def ensure_daemon(cwd: str) -> str:
    """The socket of *the* warm rector daemon — started, reused, or replaced.

    Delegates to presets/mcp/_spawn (#451): the check-and-spawn runs under an
    exclusive lock, and a daemon holding a config that no longer matches disk
    is retired rather than asked for an answer. The daemon still reads its own
    cmd from the caller .supertool.json mcp.<DAEMON_NAME> entry.
    """
    no_transport = _refusal.daemon_transport_reason()
    if no_transport:
        # Checked in this body and not at the top of `main`: the suites that
        # stub the daemon layer replace this whole function, and a check any
        # earlier short-circuits before the stub takes effect. The binary
        # lookup runs first because both outcomes are skips and "install it" is
        # the more actionable of the two. See refusal.daemon_transport_reason
        # for the full argument (#544).
        resolve_bin(cwd)
        raise _refusal.DaemonUnavailable(no_transport)
    try:
        return _spawn.ensure_daemon(
            cwd, DAEMON_NAME,
            preflight=lambda: resolve_bin(cwd),
            spawn_timeout=SPAWN_TIMEOUT_SEC,
        )
    except _spawn.AutospawnSuppressed:
        # The binary lookup runs here for the same reason it runs in the
        # no-transport arm above: both outcomes are skips, and "install it" is
        # the more actionable of the two. `_spawn` declines before its own
        # `preflight` deliberately -- a caller that may not spawn should spend
        # nothing on the spawn path -- so without this the lookup never happens
        # and the receipt advises warming a daemon for a binary that is not on
        # the machine. That is the normal case for any `cwd:` pointed at a git
        # worktree where `composer install` never ran, and it is the row
        # docs/validators.md #531 documents (#1743).
        resolve_bin(cwd)
        raise


def ndjson_call(sock_path: str, file_path: str) -> dict:
    """Initialize + tools/call(rector_process). Returns parsed MCP response dict."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(CALL_TIMEOUT_SEC)
        s.connect(sock_path)

        # initialize + notify + call — daemon bridges raw stdio so we speak JSON-RPC.
        # #1935: an unpredictable per-call id, not the fixed literal `2` --
        # see ndjson_scan.py's module docstring for what that closes.
        req_id = random.randrange(2, 2**32)  # exclude 0/1 -- 1 is the initialize frame's id
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "rector-mcp-adapter", "version": "1.0.0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": req_id, "method": "tools/call",
             "params": {"name": "rector_process",
                        "arguments": {"path": file_path, "dryRun": True}}},
        ]
        s.sendall(("\n".join(json.dumps(m) for m in msgs) + "\n").encode())

        # Read until the req_id response or EOF.
        buf = b""
        deadline = time.monotonic() + CALL_TIMEOUT_SEC
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            s.settimeout(remaining)
            try:
                chunk = s.recv(65536)
            except (socket.timeout, TimeoutError):
                break
            if not chunk:
                break
            buf += chunk
            # #1924: scan the whole buffer, not one LF-delimited line at a
            # time — a fatal rector run's HTML error page can glue the real
            # response to the end of the last HTML line with no separator,
            # and a line-anchored parser never sees it.
            obj = _ndjson_scan.find_response(buf, req_id)
            if obj is not None:
                return obj
        raise RuntimeError(
            f"no id={req_id} response within {CALL_TIMEOUT_SEC}s "
            f"({_ndjson_scan.describe_buffer(buf, req_id)})")


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
    except (_refusal.DaemonUnavailable, _spawn.AutospawnSuppressed) as e:
        # Two ways to have nothing to say, one receipt. Either the analyser is
        # not installed for this working directory — every `cwd:` into a git
        # worktree lands here — or there is no warm daemon and
        # `$SUPERTOOL_MCP_AUTOSPAWN` forbids raising a cold one (#1743). The
        # second used to be neither: the flag was stamped into this process's
        # environment and read by nothing here, so the adapter spent its whole
        # spawn budget disobeying it and the receipt never mentioned it.
        #
        # Nothing was analysed in either case, so nothing is reported — unless
        # this validator is named in `$SUPERTOOL_REQUIRE_VALIDATORS`, in which
        # case a gate that did not run says so loudly (#1202). `absent`, not
        # `skipped`, is what makes that reachable.
        print(json.dumps(_refusal.absent(
            "rector-mcp", file_path, str(e),
            int((time.monotonic() - t0) * 1000))))
        return 0
    duration_ms = int((time.monotonic() - t0) * 1000)
    print(json.dumps(format_response(file_path, resp, duration_ms)))
    return 0


if __name__ == "__main__":
    # The net used to be a nine-line `except Exception` inside `main`, wrapped
    # around `ensure_daemon` + `ndjson_call` only -- so the
    # `print(json.dumps(format_response(...)))` two lines below it was outside
    # every handler this adapter had, and an exception there left stdout empty
    # exactly as if there were no net at all. Four copies of it, one per MCP
    # adapter, differing only in the name they wrote into the payload (#1697).
    sys.exit(_refusal.guard_main("rector-mcp", main, sys.argv))
