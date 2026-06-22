"""#279: the bundled claude-channel MCP server must resolve its script
regardless of the cwd Claude Code launches it from, and the plugin manifest
version must match the code's VERSION.
"""
from __future__ import annotations

import json
from pathlib import Path

import supertool

ROOT = Path(supertool.__file__).resolve().parent


def _load(rel: str) -> dict:
    return json.loads((ROOT / rel).read_text())


# ---------------------------------------------------------------------------
# .mcp.json — plugin-root-relative path (not cwd-relative)
# ---------------------------------------------------------------------------

def test_claude_channel_uses_plugin_root_not_cwd_relative() -> None:
    cfg = _load(".mcp.json")
    args = cfg["mcpServers"]["claude-channel"]["args"]
    script = args[-1]
    # Claude Code spawns plugin MCP servers with cwd = the active project, so a
    # "./…"-relative path resolves against the wrong dir. Must use the expanded
    # plugin-root variable instead.
    assert script.startswith("${CLAUDE_PLUGIN_ROOT}/"), (
        f"claude-channel script path must be plugin-root-relative, got: {script!r}"
    )
    assert not script.startswith("./"), "path must not be cwd-relative"
    assert script.endswith("notifiers/claude-channel/channel.ts")


# ---------------------------------------------------------------------------
# version consistency — manifest vs code
# ---------------------------------------------------------------------------

def test_plugin_manifest_version_matches_code() -> None:
    manifest = _load(".claude-plugin/plugin.json")
    assert manifest["version"] == supertool.VERSION, (
        f"plugin.json version {manifest['version']!r} != "
        f"supertool.VERSION {supertool.VERSION!r}"
    )


def test_version_is_bumped_for_279() -> None:
    assert supertool.VERSION == "0.15.1"
