"""channel.ts under Node own `--experimental-strip-types` loader, for real (#2469).

#520 switched the shipped launch config -- `.mcp.json` (now `start.mjs`, which
`import`s `channel.ts`; #2486) and the `package.json` `start` script -- to
`node --experimental-strip-types`. The `notifiers` job in
`.github/workflows/tests.yml` still only ever exercises `channel.ts` under bun
(`bunx tsc --noEmit`, plus every integration test own subprocess spawn
hardcoding `["bun", ...]`). No CI leg had ever spawned the exact command real
users now run.

Node and bun are not interchangeable here: `probe()` in `channel.ts` (the
function that decides whether a stale socket file has a live listener behind
it) documents that against the identical stale socket, node `connect()`
reports `ECONNREFUSED` and bun reports `ENOENT` -- both mapped to the same
`"vacant"` verdict, but by two different branches of one `if`. A regression
that broke the node branch specifically -- the one real users exercise -- could
ship on a fully green board forever, because #550 own coverage of that exact
scenario (`test_a_genuinely_stale_socket_file_is_still_reclaimed`) has only
ever run under bun.

So this file does not re-derive a new scenario: it re-runs that one, under
node, so the branch node actually takes is the branch under test. Kept
otherwise narrow, per #2469 own scope -- one handshake, one stale-socket
reclaim, not the whole bun-covered contract duplicated onto a second runtime.

Coverage: the `notifiers` job "Run channel.ts under node for real" step runs
this file for real on ubuntu and macOS, under `SUPERTOOL_REQUIRE_JS=1` so a
missing prerequisite is a collection error instead of a silent skip (the same
#557 contract `_toolchain_gate.py` already enforces for the bun step). The
twelve-leg pytest matrix installs no JS runtime at all and skips it, with the
reason printed.
"""
from __future__ import annotations

import os
import shutil
import socket as _socket
import subprocess
import tempfile
from pathlib import Path

import pytest

from _toolchain_gate import js_promised, require_or_skip
from test_notifiers_claude_channel_550 import Channel

REPO = Path(__file__).resolve().parents[1]
CHANNEL_TS = REPO / "notifiers" / "claude-channel" / "channel.ts"
NODE_MODULES = REPO / "notifiers" / "claude-channel" / "node_modules"


def _node_version_ok() -> bool:
    """`--experimental-strip-types` was added in node 22.6.0; older nodes reject
    the flag outright, which would misreport as a channel.ts bug rather than
    an unmet prerequisite."""
    node = shutil.which("node")
    if node is None:
        return False
    try:
        out = subprocess.run([node, "--version"], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=10).stdout.strip()
        major, minor, *_ = out.lstrip("v").split(".")
        return (int(major), int(minor)) >= (22, 6)
    except Exception:
        return False


pytestmark = [
    require_or_skip(
        hasattr(_socket, "AF_UNIX"),
        "claude-channel binds an AF_UNIX socket — not available on this platform",
        promised=js_promised(),
    ),
    require_or_skip(
        _node_version_ok(),
        "claude-channel ships node --experimental-strip-types; "
        "no node >= 22.6 on PATH",
        promised=js_promised(),
    ),
    require_or_skip(
        NODE_MODULES.exists(),
        "channel deps not installed — run notifiers/claude-channel/install.sh",
        promised=js_promised(),
    ),
]

NODE_CMD = ["node", "--experimental-strip-types", str(CHANNEL_TS)]


@pytest.fixture()
def sock_path():
    # macOS caps AF_UNIX paths at ~104 bytes; keep it under /tmp, not tmp_path.
    d = tempfile.mkdtemp(prefix="st2469-", dir="/tmp")
    try:
        yield os.path.join(d, "w.sock")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_a_real_handshake_over_the_shipped_node_command(sock_path: str) -> None:
    """The exact command `.mcp.json`/`start.mjs` now launch, spoken to for real."""
    ch = Channel(sock_path, cmd=NODE_CMD)
    try:
        ch.emit({
            "ts": "2026-09-09T00:00:00Z", "source": "gitlab-mr", "id": "2469",
            "event": "pipeline_succeeded", "payload": {"title": "node handshake"},
        })
        msg = ch.next_message()
        assert msg["params"]["meta"]["id"] == "2469"
    finally:
        ch.close()


def test_a_genuinely_stale_socket_file_is_reclaimed_under_node(sock_path: str) -> None:
    """The exact #550 scenario, run under the runtime whose `probe()` branch
    (`ECONNREFUSED`, not bun ENOENT) it is meant to exercise."""
    dead = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    dead.bind(sock_path)
    dead.listen(1)
    dead.close()  # file survives; connect() to it now gives ECONNREFUSED
    assert os.path.exists(sock_path)

    ch = Channel(sock_path, cmd=NODE_CMD)
    try:
        ch.emit({
            "ts": "2026-09-09T00:00:00Z", "source": "gitlab-mr", "id": "2469b",
            "event": "pipeline_succeeded", "payload": {"title": "after a crash"},
        })
        assert ch.next_message()["params"]["meta"]["id"] == "2469b"
    finally:
        ch.close()
