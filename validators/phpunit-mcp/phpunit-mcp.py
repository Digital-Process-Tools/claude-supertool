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
import pathlib
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from shutil import which

DAEMON_NAME = os.environ.get("MCP_PHPUNIT_DAEMON_NAME", "phpunit-warm")
DAEMON_PROC = os.environ.get("MCP_PHPUNIT_BIN", "mcp-phpunit-warm")
WORKING_DIR = os.environ.get("MCP_PHPUNIT_WORKING_DIR", os.getcwd())
SPAWN_TIMEOUT_SEC = 30
CALL_TIMEOUT_SEC = 300


def sock_paths(cwd: str, name: str) -> tuple[str, str]:
    h = hashlib.sha1(f"{cwd}::{name}".encode()).hexdigest()[:12]
    base = f"/tmp/supertool-mcp-{h}"
    return f"{base}.sock", f"{base}.pid"


def is_alive(pid_path: str) -> bool:
    try:
        with open(pid_path) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def ensure_daemon(cwd: str) -> str:
    sock, pid = sock_paths(cwd, DAEMON_NAME)
    if os.path.exists(sock) and is_alive(pid):
        return sock

    bin_path = DAEMON_PROC
    if not os.path.isabs(bin_path):
        resolved = which(bin_path)
        if resolved is None:
            raise RuntimeError(
                f"mcp-phpunit-warm not found on $PATH. Install via: composer global require dpt/mcp-phpunit-warm\n"
                f"Or set MCP_PHPUNIT_BIN=/abs/path/to/mcp-phpunit-warm."
            )
        bin_path = resolved

    # daemon.py lives in supertool's presets/mcp/. Find it relative to this adapter file.
    supertool_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    daemon_script = os.path.join(supertool_root, "presets/mcp/daemon.py")
    if not os.path.isfile(daemon_script):
        raise RuntimeError(f"daemon.py not found: {daemon_script}")

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


def source_context(file_path: str, error_line: int | None) -> list[str]:
    if error_line is None:
        return []
    try:
        lines = pathlib.Path(file_path).read_text(errors="replace").splitlines()
    except OSError:
        return []
    ctx = []
    for offset in range(-2, 3):
        ln = error_line + offset
        if 1 <= ln <= len(lines):
            prefix = f"{ln}\u2192" if offset == 0 else f"{ln}:"
            ctx.append(f"{prefix} {lines[ln - 1]}")
    return ctx


def parse_junit(file_path: str, junit_xml: str, dur_ms: int) -> dict:
    base = {"tool": "phpunit-mcp", "file": file_path, "ok": True, "count": 0,
            "errors": [], "duration_ms": dur_ms}
    try:
        root = ET.fromstring(junit_xml)
    except ET.ParseError as e:
        base["ok"] = False
        base["count"] = 1
        base["errors"] = [{"line": None, "col": None, "severity": "error",
                           "code": "adapter", "msg": f"junit parse: {e}"}]
        return base

    tests_total = failures = errors_n = skipped = assertions = 0
    err_list = []
    for ts in root.iter("testsuite"):
        has_direct_cases = any(child.tag == "testcase" for child in ts)
        if not has_direct_cases:
            continue
        tests_total += int(ts.get("tests", 0) or 0)
        failures += int(ts.get("failures", 0) or 0)
        errors_n += int(ts.get("errors", 0) or 0)
        skipped += int(ts.get("skipped", 0) or 0)
        assertions += int(ts.get("assertions", 0) or 0)
        for tc in ts.iter("testcase"):
            for tag in ("failure", "error"):
                el = tc.find(tag)
                if el is None:
                    continue
                msg = (el.get("message") or el.text or "").strip().splitlines()[0]
                line = tc.get("line")
                try:
                    line_int = int(line) if line else None
                except ValueError:
                    line_int = None
                err_list.append({
                    "line": line_int,
                    "col": None,
                    "severity": "error",
                    "code": f"phpunit.{tag}",
                    "msg": f"{tc.get('name', '?')}: {msg}",
                    "source_context": source_context(file_path, line_int),
                })

    fail_total = failures + errors_n
    base["ok"] = fail_total == 0
    base["count"] = fail_total
    base["errors"] = err_list
    base["metrics"] = {
        "tests_total": tests_total,
        "tests_passed": tests_total - fail_total - skipped,
        "tests_skipped": skipped,
        "assertions": assertions,
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
    junit_xml = structured.get("output", "") or ""
    exit_code = structured.get("exit_code", 0)

    if junit_xml.strip().startswith("<"):
        return parse_junit(file_path, junit_xml, dur_ms)

    if exit_code != 0:
        base["ok"] = False
        base["count"] = 1
        base["errors"] = [{"line": None, "col": None, "severity": "error",
                           "code": "phpunit.exit",
                           "msg": f"phpunit exit {exit_code} with no junit output"}]
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
    except Exception as e:
        import traceback
        tb = traceback.format_exc().splitlines()[-3:]
        print(json.dumps({
            "tool": "phpunit-mcp", "file": file_path, "ok": False, "count": 1,
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
