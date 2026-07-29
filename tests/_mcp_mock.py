"""Spawn + readiness helper for the mock MCP server fixture (#491).

Three test modules (`test_mcp_client`, `test_mcp_routing`, `test_mcp_workspace`)
each carried their own copy of the same fixture, and each copy waited for the
socket *file* to appear before handing the path to a client. One helper, one
readiness rule — see `wait_until_ready` for why the file is the wrong thing to
wait for.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Optional, Tuple

MOCK_SERVER = str(Path(__file__).parent / "fixtures" / "mock_mcp_server.py")

# Sockets live in /tmp/ rather than under tmp_path: macOS caps an AF_UNIX path
# at ~104 bytes and a pytest tmp_path is already most of that.
READY_TIMEOUT_SEC = 10.0


def socket_path(prefix: str = "st-mock") -> str:
    """A fresh per-test socket path."""
    return f"/tmp/{prefix}-{uuid.uuid4().hex[:8]}.sock"


def wait_until_ready(
    path: str,
    proc: Optional[subprocess.Popen] = None,
    timeout: float = READY_TIMEOUT_SEC,
) -> str:
    """Block until the mock server at ``path`` is ready to serve a client.

    Readiness is a successful connect, not an existing file (#491). `bind()`
    publishes the socket path and `listen()` is what makes a connect succeed;
    a client that lands between the two gets ECONNREFUSED against a path that
    exists. `MCPClient` given an explicit `socket_path` connects exactly once
    and raises on a miss — deliberately, since nobody else will bind a test
    path (#475/#488) — so the fixture has to have closed that window before it
    hands the path over. The window is sub-millisecond idle and unbounded on a
    loaded runner under `-n auto`, which is why this surfaced as an hourly
    flake on macOS CI rather than never.

    Probing by connect is also the only check that reports a server which died
    during startup: `proc.poll()` turns a ten-second wait into an immediate,
    named failure.
    """
    deadline = time.monotonic() + timeout
    last: Optional[OSError] = None
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(
                f"mock MCP server exited with code {proc.returncode} "
                f"before accepting on {path}"
            )
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(1.0)
            probe.connect(path)
            return path
        except OSError as exc:
            last = exc
            time.sleep(0.02)
        finally:
            probe.close()
    raise RuntimeError(
        f"mock MCP server did not accept connections on {path} "
        f"within {timeout}s (last error: {last!r})"
    )


def spawn(
    sock_path: Optional[str] = None,
    env: Optional[dict] = None,
    prefix: str = "st-mock",
    timeout: float = READY_TIMEOUT_SEC,
) -> Tuple[subprocess.Popen, str]:
    """Start the mock server and return once it is ready to serve a client."""
    path = sock_path or socket_path(prefix)
    proc = subprocess.Popen([sys.executable, MOCK_SERVER, path], env=env)
    try:
        wait_until_ready(path, proc, timeout)
    except BaseException:
        terminate(proc, path)
        raise
    return proc, path


def terminate(proc: subprocess.Popen, path: Optional[str] = None) -> None:
    """Stop the mock server and remove its socket."""
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
    if path and os.path.exists(path):
        try:
            os.unlink(path)
        except OSError:
            pass


def skip_without_af_unix() -> None:
    """Skip the calling test on platforms with no AF_UNIX (some Windows builds)."""
    if not hasattr(socket, "AF_UNIX"):
        import pytest
        pytest.skip("MCP daemon uses AF_UNIX sockets — not available on this platform")
