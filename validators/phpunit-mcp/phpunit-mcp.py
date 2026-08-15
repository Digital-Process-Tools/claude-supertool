#!/usr/bin/env python3
"""PHPUnit validator via warm MCP daemon.

Usage: phpunit-mcp.py FILE

Connects to the long-lived mcp-phpunit-warm daemon over UDS. Auto-spawns on first call.
Daemon name + working dir + phpunit config are read from $MCP_PHPUNIT_* env vars (set by
the `cmd` template in .supertool.json), with sensible fallbacks.

Output: SCHEMA.md-compliant JSON on stdout (single line).
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import time
from shutil import which

DAEMON_NAME = os.environ.get("MCP_PHPUNIT_DAEMON_NAME", "phpunit-warm")
DAEMON_PROC = os.environ.get("MCP_PHPUNIT_BIN", "mcp-phpunit-warm")
WORKING_DIR = os.environ.get("MCP_PHPUNIT_WORKING_DIR", os.getcwd())
SPAWN_TIMEOUT_SEC = 30
CALL_TIMEOUT_SEC = 300
# Cap each error message — phpunit assertion-on-HTML failures can dump
# multi-megabyte string diffs that would blow up the validator output
# (observed 2M+ tokens on a single PageIndexDelegate test failure).
MSG_MAX_CHARS = int(os.environ.get("PHPUNIT_MCP_MSG_MAX_CHARS", "2000"))


def _cap_msg(msg: str) -> str:
    """Truncate a single error message to MSG_MAX_CHARS with an ellipsis hint."""
    if len(msg) <= MSG_MAX_CHARS:
        return msg
    head = MSG_MAX_CHARS - 80
    return (
        msg[:head]
        + f"... [TRUNCATED — {len(msg) - head} more chars; "
        + "raise PHPUNIT_MCP_MSG_MAX_CHARS or run phpunit directly to see full]"
    )


# #148: use the shared presets/mcp/_paths helper so client + daemon agree on
# the runtime dir (was /tmp/, now $XDG_RUNTIME_DIR/supertool/mcp/ etc.).
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent.parent / "presets" / "mcp"))
from _paths import socket_pid_paths as _shared_socket_pid_paths  # noqa: E402
import _spawn  # noqa: E402  (#451: one daemon per (kind, config fingerprint))

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "common"))
import refusal as _refusal  # noqa: E402
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
                    f"mcp-phpunit-warm not found at: {candidate}")
            bin_path = candidate
        else:
            resolved = which(bin_path)
            if resolved is None:
                raise _refusal.DaemonUnavailable(
                    "mcp-phpunit-warm not found on $PATH — install via: "
                    "composer global require dpt/mcp-phpunit-warm, or set "
                    "MCP_PHPUNIT_BIN (abs, or relative to the project root)."
                )
            bin_path = resolved
    return bin_path


def ensure_daemon(cwd: str) -> str:
    """The socket of *the* warm phpunit daemon — started, reused, or replaced.

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


def ndjson_call(sock_path: str, test_file: str) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(CALL_TIMEOUT_SEC)
        s.connect(sock_path)
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "phpunit-mcp-adapter", "version": "1.0.0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "phpunit_run",
                        "arguments": {"testFile": test_file}}},
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

    # v0.2.0+: output is always JSON from InMemorySubscriber
    if output.strip().startswith("{"):
        return parse_json_output(file_path, output, dur_ms)

    # Fallback for empty output with non-zero exit (e.g. config error before any test ran)
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
            "phpunit-mcp", file_path, str(e),
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
    sys.exit(_refusal.guard_main("phpunit-mcp", main, sys.argv))
