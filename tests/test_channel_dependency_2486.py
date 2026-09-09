"""A fresh plugin install must be able to start the channel consumer (#2486).

`.mcp.json` declares the consumer for every plugin install (#520), and
`channel.ts` imports `@modelcontextprotocol/sdk` at load. `node_modules/` is
gitignored, nothing in a plugin install runs `install.sh`, and `package.json`
declares no `postinstall` -- so the declared server died on
`ERR_MODULE_NOT_FOUND` for anybody who installed fresh. Found by the v0.59.0
release audit, round 2.

The constraint that shapes the fix, and the reason a launcher cannot simply
shell out and forget: **stdout is the JSON-RPC stream**. A single line of npm
progress on stdout corrupts the MCP handshake, which fails in the one place a
session cannot see.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CHANNEL_DIR = REPO / "notifiers" / "claude-channel"


def _mcp() -> dict:
    return json.loads((REPO / ".mcp.json").read_text(encoding="utf-8"))


def test_the_declared_entry_point_guarantees_the_dependency() -> None:
    """The path `.mcp.json` names must be one that ensures the SDK is present.

    `channel.ts` cannot: it imports the SDK at module load, so by the time any
    code of its own runs, the import has already failed.
    """
    server = _mcp()["mcpServers"]["claude-channel"]
    entry = [a for a in server["args"] if not a.startswith("-")]
    assert entry, server["args"]
    named = Path(entry[-1]).name
    assert named != "channel.ts", (
        "`.mcp.json` points straight at channel.ts, which imports the SDK at "
        "load -- on a fresh plugin install there is no node_modules and the "
        "server dies before any of its own code runs (#2486)"
    )
    launcher = CHANNEL_DIR / named
    assert launcher.is_file(), f"{named} is declared but not in the tree"


def test_the_launcher_never_writes_to_stdout() -> None:
    """stdout is the protocol. The launcher may print diagnostics, and every
    one of them has to go to stderr.

    Asserted against the source rather than by running it, because the failing
    path needs a missing dependency and a network, and neither belongs in this
    test.
    """
    server = _mcp()["mcpServers"]["claude-channel"]
    entry = [a for a in server["args"] if not a.startswith("-")][-1]
    source = (CHANNEL_DIR / Path(entry).name).read_text(encoding="utf-8")
    assert "console.log(" not in source, (
        "console.log writes to stdout, which is the JSON-RPC stream -- use "
        "console.error, whose bytes go to stderr where the harness logs them"
    )
    assert "process.stdout" not in source, (
        "the launcher must not touch process.stdout: it belongs to the "
        "protocol, and channel.ts's own transport owns it"
    )


def test_a_missing_dependency_that_cannot_be_installed_says_so_by_name(
        tmp_path) -> None:
    """The third state. An install that cannot run must produce a sentence
    naming the remedy, not a module-resolution stack trace.

    Run with npm forced to fail (an empty PATH entry shadowing it), against a
    copy of the launcher in a tree with no node_modules -- so nothing here
    reaches the network, and the assertion is about the message rather than
    about the install succeeding.
    """
    if not (CHANNEL_DIR / "start.mjs").is_file():
        pytest.skip("no launcher yet -- the test above is the one that fails")
    sandbox = tmp_path / "claude-channel"
    sandbox.mkdir()
    for name in ("start.mjs", "package.json"):
        (sandbox / name).write_text(
            (CHANNEL_DIR / name).read_text(encoding="utf-8"), encoding="utf-8")
    (sandbox / "channel.ts").write_text("export {};\n", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    npm = fake_bin / "npm"
    npm.write_text("#!/bin/sh\nexit 127\n", encoding="utf-8")
    npm.chmod(0o755)

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH here -- CI's own legs cover this")
    # The fake bin comes first so its `npm` wins, and node's own directory
    # follows so the interpreter is still findable: stripping PATH entirely
    # made this test fail on a missing `node` rather than on the message.
    path = os.pathsep.join([str(fake_bin), str(Path(node).parent)])
    proc = subprocess.run(
        [node, "--experimental-strip-types", str(sandbox / "start.mjs")],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120, env={"PATH": path, "HOME": str(tmp_path)},
    )

    assert proc.returncode != 0, "a launcher that could not install must fail"
    assert "ERR_MODULE_NOT_FOUND" not in proc.stderr, (
        "the raw resolution error reached the operator instead of a sentence "
        "naming the remedy: " + proc.stderr[-400:]
    )
    assert "install.sh" in proc.stderr or "npm install" in proc.stderr, (
        "the failure names no remedy: " + proc.stderr[-400:]
    )
    assert proc.stdout == "", (
        "the launcher wrote to stdout, which is the JSON-RPC stream: "
        + proc.stdout[:200]
    )


def test_bun_lock_is_gone_now_that_bun_is_not_the_runtime() -> None:
    """#520 dropped Bun; the lockfile it needs outlived it and is still
    tracked. Noted by the same audit round."""
    tracked = subprocess.run(
        ["git", "ls-files", "notifiers/claude-channel/bun.lock"],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=30).stdout.strip()
    assert not tracked, (
        "notifiers/claude-channel/bun.lock is still tracked after the Bun "
        "runtime was dropped (#520) -- a lockfile for a runtime nothing uses"
    )
