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
import random
import socket
import sys
import time

# Reuse the shared 5-line source-context helper (same one the cold phpmd adapter uses).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "common"))
from source_context import context_fields  # noqa: E402

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
import ndjson_scan as _ndjson_scan  # noqa: E402  (#1924: a response glued to noise)

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
    """Initialize + tools/call(phpmd_analyse). Returns parsed MCP response dict."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(CALL_TIMEOUT_SEC)
        s.connect(sock_path)

        # #1935: an unpredictable per-call id, not the fixed literal `2` --
        # see ndjson_scan.py's module docstring for what that closes.
        req_id = random.randrange(2, 2**32)  # exclude 0/1 -- 1 is the initialize frame's id
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "phpmd-mcp-adapter", "version": "1.0.0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": req_id, "method": "tools/call",
             "params": {"name": "phpmd_analyse",
                        "arguments": {"path": file_path}}},
        ]
        s.sendall(("\n".join(json.dumps(m) for m in msgs) + "\n").encode())

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
            # time — a fatal analysis run's HTML error page can glue the real
            # response to the end of the last HTML line with no separator,
            # and a line-anchored parser never sees it.
            obj = _ndjson_scan.find_response(buf, req_id)
            if obj is not None:
                return obj
        raise RuntimeError(
            f"no id={req_id} response within {CALL_TIMEOUT_SEC}s "
            f"({_ndjson_scan.describe_buffer(buf, req_id)})")


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

    output = structured.get("output", "") or ""
    unreadable = False
    try:
        report = json.loads(output) if output else {}
    except json.JSONDecodeError:
        # Deliberately not an early return: the error key below is the better
        # message when both arrived, and it used to win because it was read
        # first. Reordering the parse ahead of it must not silently demote a
        # named runtime error to "could not parse".
        report = {}
        unreadable = True
    if unreadable and not structured.get("error"):
        base["ok"] = False
        base["count"] = 1
        base["errors"] = [{"line": None, "col": None, "severity": "error",
                           "code": "phpmd.parse", "msg": "could not parse PHPMD JSON output"}]
        return base

    # The tool returns a SecurityError / runtime error as an extra key. It used
    # to `return` here, discarding `structured["output"]` unread — so if the
    # daemon ever set both, a report it produced was replaced by a message about
    # it (#1547). Whether it can set both is not answerable from this repo: the
    # server is `mcp-phpmd-warm` and lives elsewhere. So the adapter is made not
    # to depend on the answer — the report is parsed first and is never dropped.
    if structured.get("error"):
        # A PHPMD report has TWO bodies and both are rendered below: the
        # violations under `files[]`, and `report["errors"]` — the processing
        # failures (an unparseable PHP file, a broken ruleset). Reading only the
        # first half would throw the second half away for the refusal, which is
        # this same discard one key over.
        has_report = bool(report.get("errors")) or any(
            (f or {}).get("violations") for f in (report.get("files", []) or []))
        # A scope refusal is not a runtime error — it is an absence of analysis,
        # and counting it as one error inflates the delta by +1 (#406). But a
        # refusal beside a report is two mutually exclusive claims, and only one
        # of them carries evidence: the same rule #1527 applied to the cold
        # phpstan adapter. The report wins, and the refusal is dropped rather
        # than counted, because a declination that did not happen is not a
        # finding about the file either.
        if _refusal.is_refusal(str(structured["error"]), SKIP_PATTERNS_ENV):
            if not has_report:
                return _refusal.skipped("phpmd-mcp", file_path,
                                        str(structured["error"]), duration_ms)
        else:
            # A genuine runtime error stays one error whether or not a report
            # arrived with it — the same accounting as the error-alone arm, so
            # the count `_validator_regressed` reads is never a guess.
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
            "phpmd-mcp", file_path, str(e),
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
    sys.exit(_refusal.guard_main("phpmd-mcp", main, sys.argv))
