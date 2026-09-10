"""#2475: `.mcp.json`'s claude-channel entry is read at project scope too,
where ${CLAUDE_PLUGIN_ROOT} is undefined, and `claude mcp list` reports
`Missing environment variables: CLAUDE_PLUGIN_ROOT`.

`.mcp.json` is the same file at both scopes -- shipped as the plugin's own
config (where ${CLAUDE_PLUGIN_ROOT} is defined by the harness) and read
directly as project config by anyone with a checkout of this repo open (where
it is not). Since #2221, `.claude/settings.json`'s `disabledMcpjsonServers`
already stops the server from actually *starting* at project scope, to avoid
the two-consumer socket collision documented at #2051 -- but that setting
only prevents a spawn, it does not stop `claude mcp list` from validating the
declared substitution and warning about the missing variable.

Claude Code's `${VAR:-default}` expansion syntax (mcp.md, "Environment
variable expansion in .mcp.json") supplies a fallback when the variable is
undefined, which both suppresses the warning and happens to be *correct*
here: Claude Code spawns a project-scope MCP server with cwd set to the
active project (#279's own comment cites this), and at project scope the
active project *is* this repo checkout -- so a `.`-relative fallback resolves
to the right file. The #2221 disable still stops it from actually running,
so this does not reopen the #2051 collision; it only removes a spurious
warning about a substitution that was never going to matter.
"""
from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MCP_JSON_PATH = REPO_ROOT / ".mcp.json"


def _mcp_json() -> dict:
    return json.loads(MCP_JSON_PATH.read_text(encoding="utf-8"))


def _script() -> str:
    return _mcp_json()["mcpServers"]["claude-channel"]["args"][-1]


def test_claude_channel_script_path_has_a_project_scope_fallback() -> None:
    """The undefined-at-project-scope variable must carry a `:-default` so
    `claude mcp list` never reports it missing there."""
    script = _script()
    assert script.startswith("${CLAUDE_PLUGIN_ROOT:-"), (
        f"claude-channel's script path has no fallback for an undefined "
        f"CLAUDE_PLUGIN_ROOT (project scope), got: {script!r} -- `claude mcp "
        f"list` reports 'Missing environment variables: CLAUDE_PLUGIN_ROOT' "
        f"for exactly this shape (#2475)"
    )


def test_the_fallback_resolves_to_the_real_script_from_repo_root() -> None:
    """The fallback must actually point at the right file once substituted at
    project scope, where cwd is this repo's own root -- not just silence the
    warning while pointing nowhere."""
    script = _script()
    assert script.startswith("${CLAUDE_PLUGIN_ROOT:-") and "}" in script, script
    fallback = script[len("${CLAUDE_PLUGIN_ROOT:-"):script.index("}")]
    rest = script[script.index("}") + 1:]
    resolved = (REPO_ROOT / (fallback + rest)).resolve()
    expected = (REPO_ROOT / "notifiers" / "claude-channel" / "start.mjs").resolve()
    assert resolved == expected, (resolved, expected)
    assert resolved.is_file(), f"fallback path does not resolve to a real file: {resolved}"


def test_claude_channel_still_disabled_at_project_scope() -> None:
    """The fallback makes the entry *resolvable* at project scope, which is
    exactly the shape #2051/#2221 warn about (two consumers racing the same
    socket) if it were ever allowed to actually start. Confirm the #2221
    disable is still in place so this fix does not reopen that collision."""
    settings = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    disabled = settings.get("disabledMcpjsonServers", [])
    assert "claude-channel" in disabled, (
        "the #2221 disable for claude-channel is gone -- the fallback added "
        "for #2475 makes this entry resolvable at project scope again, which "
        "would reopen the #2051 two-consumer collision if nothing disables it"
    )
