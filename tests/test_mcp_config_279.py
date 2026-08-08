"""#279: the bundled claude-channel MCP server must resolve its script
regardless of the cwd Claude Code launches it from, and the plugin manifest
version must match the code's VERSION.

Every published statement of the version is pinned to `supertool.VERSION` here,
including the README badge — which drifted from 0.14.1 to a 0.29.0 release
precisely because it was the one version site with no test behind it.
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
