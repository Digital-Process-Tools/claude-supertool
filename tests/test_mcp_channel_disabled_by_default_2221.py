"""A fresh clone gets `.mcp.json`'s `claude-channel` declaration and not the
disable that resolves it, so the two-server collision is the default state
(#2221).

`.mcp.json` declares `claude-channel` unconditionally (#1541) -- it ships with
the plugin and is also read directly as project config by anyone working in
this checkout. `bin/oss-workspace`, in the separate `oss` plugin, registers a
second server (`oss-channel`) at local scope on session-open, resolving to the
same `notifiers/claude-channel/channel.ts` on one Unix socket. Both configured,
one binds and the other is silently refused, and `channel:health` can only
report `CANNOT DETERMINE` (docs/presets/watch.md's own collision table,
#2051/#2133).

What stops this on the maintainer's own machine is `disabledMcpjsonServers:
["claude-channel"]` in `.claude/settings.local.json` -- gitignored, so it does
not travel to a fresh clone. This file pins the fix: the same disable, tracked
in `.claude/settings.json` instead, so every developer working on this
repository gets it from the checkout rather than re-deriving it by hand -- the
same reasoning `tests/test_statusline_wiring_documented_1964.py` already
applies to `statusLine`.

Route not taken: deleting `claude-channel` from `.mcp.json` outright. That
file also ships with the plugin (`${CLAUDE_PLUGIN_ROOT}`-relative args) and is
the only registration path for a plugin user who never runs `oss-workspace` --
removing it would drop channel support for them entirely, which is a feature
regression rather than a sweep. Disabling it project-locally, in the tracked
settings of *this* checkout, resolves the collision here without touching
what every other install receives.
"""
from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = REPO_ROOT / ".claude" / "settings.json"
MCP_JSON_PATH = REPO_ROOT / ".mcp.json"


def _settings() -> dict:
    return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))


def _mcp_json() -> dict:
    return json.loads(MCP_JSON_PATH.read_text(encoding="utf-8"))


def test_mcp_json_still_declares_claude_channel() -> None:
    """Sanity check for the fixture the test below depends on: if this ever
    goes false, the collision this fix resolves no longer exists and the
    disable below is inert rather than proven."""
    assert "claude-channel" in _mcp_json().get("mcpServers", {}), (
        ".mcp.json no longer declares claude-channel -- #2221's disable has "
        "nothing left to resolve, and test_disabled_mcpjson_servers_disables_"
        "claude_channel is vacuous rather than failing for the right reason"
    )


def test_disabled_mcpjson_servers_disables_claude_channel() -> None:
    """The tracked settings must disable the server that collides with
    `oss-workspace`'s `oss-channel`, so a fresh clone gets the resolution
    that today only exists on the maintainer's own untracked
    `.claude/settings.local.json`."""
    settings = _settings()
    assert "disabledMcpjsonServers" in settings, (
        ".claude/settings.json does not disable any MCP server (#2221) -- a "
        "fresh clone gets .mcp.json's claude-channel declaration and "
        "bin/oss-workspace's oss-channel racing for the same socket, and "
        "channel:health can only report CANNOT DETERMINE"
    )
    disabled = settings["disabledMcpjsonServers"]
    assert isinstance(disabled, list) and "claude-channel" in disabled, (
        "disabledMcpjsonServers is tracked but does not disable claude-channel: "
        + repr(disabled)
    )
