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
import random
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
import ndjson_scan as _ndjson_scan  # noqa: E402  (#1924: a response glued to noise)
from source_context import context_fields  # noqa: E402


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
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(CALL_TIMEOUT_SEC)
        s.connect(sock_path)
        # #1935: an unpredictable per-call id, not the fixed literal `2` --
        # see ndjson_scan.py's module docstring for what that closes.
        req_id = random.randrange(2, 2**32)  # exclude 0/1 -- 1 is the initialize frame's id
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "phpstan-mcp-adapter", "version": "1.0.0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": req_id, "method": "tools/call",
             "params": {"name": "phpstan_analyse",
                        "arguments": {"path": file_path}}},
        ]
        s.sendall(("\n".join(json.dumps(m) for m in msgs) + "\n").encode())
        # #1924: scans the whole buffer, not one LF-delimited line at a
        # time — a fatal analysis run's HTML error page can glue the real
        # response to the end of the last HTML line with no separator, and a
        # line-anchored parser never sees it. #1927: gives up on idle
        # silence rather than waiting out the whole call budget, and names
        # what was received (or the daemon's own log) on a timeout instead
        # of only that one happened.
        return _ndjson_scan.receive_until(s, req_id, CALL_TIMEOUT_SEC, sock_path)


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
    out_of_scope = _refusal.outside_roots(file_path, PATHS_ENV)
    if out_of_scope:
        print(json.dumps(skipped(file_path, out_of_scope,
                                 int((time.monotonic() - t0) * 1000))))
        return 0
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
            "phpstan-mcp", file_path, str(e),
            int((time.monotonic() - t0) * 1000))))
        return 0
    dur_ms = int((time.monotonic() - t0) * 1000)
    print(json.dumps(format_response(file_path, resp, dur_ms)))
    return 0


if __name__ == "__main__":
    # The net used to be a nine-line `except Exception` inside `main`, wrapped
    # around `ensure_daemon` + `ndjson_call` only -- so the
    # `print(json.dumps(format_response(...)))` two lines below it was outside
    # every handler this adapter had, and an exception there left stdout empty
    # exactly as if there were no net at all. Four copies of it, one per MCP
    # adapter, differing only in the name they wrote into the payload (#1697).
    sys.exit(_refusal.guard_main("phpstan-mcp", main, sys.argv))
