"""#279: the bundled claude-channel MCP server must resolve its script
regardless of the cwd Claude Code launches it from, and the plugin manifest
version must match the code's VERSION.

Every published statement of the version is pinned to `supertool.VERSION` here,
including the README badge — which drifted from 0.14.1 to a 0.29.0 release
precisely because it was the one version site with no test behind it.

#520 added the two blocks below: the plugin manifest must declare
`claude-channel` as a channel, and the consumer it launches must be the
Bun-free runtime this repo now ships (`node --experimental-strip-types`), not
`bun` — the whole point of dropping the Bun install requirement is lost if the
declared command still names it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import supertool

ROOT = Path(supertool.__file__).resolve().parent


def _load(rel: str) -> dict:
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


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
    # start.mjs since #2486, not channel.ts: the consumer imports the SDK at
    # module load, so on a fresh plugin install -- where node_modules is
    # gitignored and nothing runs install.sh -- naming channel.ts here meant
    # the declared server died before any of its own code ran.
    assert script.endswith("notifiers/claude-channel/start.mjs")


# ---------------------------------------------------------------------------
# #520 — declared channel, Bun-free runtime
# ---------------------------------------------------------------------------

def test_plugin_manifest_declares_claude_channel_as_a_channel() -> None:
    """`channels` in plugin.json is what makes this plugin *structurally* a
    channel provider — it does not on its own exempt a user from
    `--dangerously-load-development-channels` (allowlist membership decides
    that, not the manifest), but without it the plugin never declares itself
    as one at all."""
    manifest = _load(".claude-plugin/plugin.json")
    channels = manifest.get("channels")
    assert channels, (
        "plugin.json declares no `channels` key — the plugin never announces "
        "itself as a channel provider"
    )
    servers = {c.get("server") for c in channels}
    assert "claude-channel" in servers, (
        f"plugin.json's channels don't name the mcpServers key they declare: "
        f"{servers!r}"
    )
    # The name a `channels` entry points at must actually be a declared server,
    # or the declaration is a dangling reference nothing backs.
    mcp = _load(".mcp.json")
    assert "claude-channel" in mcp["mcpServers"], (
        ".mcp.json declares no `claude-channel` server for plugin.json's "
        "channels entry to point at"
    )


def test_claude_channel_runs_under_node_not_bun() -> None:
    """#520: channel.ts makes zero `Bun.*` calls, so shipping `"command": "bun"`
    bought nothing but an install step that fails silently for anyone without
    it. The declared command must be the runtime this repo actually documents
    installing — `node`, not `bun`."""
    cfg = _load(".mcp.json")
    server = cfg["mcpServers"]["claude-channel"]
    assert server["command"] == "node", (
        f"claude-channel's declared command is {server['command']!r}, not "
        "'node' — #520 dropped the Bun requirement from the shipped runtime"
    )
    assert "--experimental-strip-types" in server["args"], (
        "node needs --experimental-strip-types to load a .ts file directly on "
        "the oldest node version this repo documents supporting (22.6.0); "
        "omitting it works only on a node new enough to strip types by "
        "default and silently breaks on an older-but-still-supported one"
    )


def test_channel_ts_still_makes_zero_bun_api_calls() -> None:
    """The regression guard for the issue's own audit finding: channel.ts had
    zero `Bun.*` calls when #520 dropped the Bun requirement from the shipped
    runtime, which is what made the drop safe. A future edit that reaches for
    a Bun-only API would silently reintroduce the exact install-time failure
    mode this issue removed — silent everywhere but on a Bun-equipped machine."""
    src = (ROOT / "notifiers" / "claude-channel" / "channel.ts").read_text(encoding="utf-8")
    hits = re.findall(r"\bBun\.\w+", src)
    assert not hits, (
        f"channel.ts now calls Bun-only API(s) {hits!r}, but the shipped "
        "runtime (.mcp.json, install.sh) is plain node — see #520"
    )


# ---------------------------------------------------------------------------
# version consistency — manifest vs code
# ---------------------------------------------------------------------------

def test_plugin_manifest_version_matches_code() -> None:
    manifest = _load(".claude-plugin/plugin.json")
    assert manifest["version"] == supertool.VERSION, (
        f"plugin.json version {manifest['version']!r} != "
        f"supertool.VERSION {supertool.VERSION!r}"
    )


_README_BADGE_RE = re.compile(
    r"!\[Version\]\(https://img\.shields\.io/badge/version-(\d+\.\d+\.\d+)-"
)


def test_readme_version_badge_matches_code() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    shown = _README_BADGE_RE.findall(readme)
    # Three states, not two: a pattern that matched nothing has not cleared the
    # badge, it has failed to look at it. Zero matches is a finding, not a pass.
    assert shown, (
        "README.md declares no version badge this test can read. If the badge "
        "was removed on purpose, remove this test with it; an empty match must "
        "never read as agreement."
    )
    for version in shown:
        assert version == supertool.VERSION, (
            f"README version badge says {version!r} != "
            f"supertool.VERSION {supertool.VERSION!r}"
        )
