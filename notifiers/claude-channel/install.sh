#!/usr/bin/env bash
# Install the claude-channel MCP server for the supertool 'watch' preset.
#
# - Installs the npm dependency (@modelcontextprotocol/sdk) with no dev
#   dependencies, so a runtime install pulls in nothing that only a
#   contributor building/type-checking this file needs.
# - Prints next-step instructions for registering the channel with Claude Code
#
# Runtime: plain `node` (#520) — no Bun install, no build step. `channel.ts`
# makes zero `Bun.*` calls; `--experimental-strip-types` strips its type
# annotations at load time. That flag is a no-op on a Node new enough to do
# this by default (observed clean, no warning, on Node 22.22.1) and is the
# documented way to get the same behavior back to Node 22.6, where the
# feature existed but was not yet on by default. Below that floor, node
# cannot run this file at all and this script refuses rather than failing
# silently later.
#
# Usage:
#   bash notifiers/claude-channel/install.sh
#
# Idempotent — safe to re-run.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v node >/dev/null 2>&1; then
    echo "ERROR: node is required but not on PATH."
    echo "Install: https://nodejs.org/ (22.6.0 or later)"
    exit 1
fi

NODE_VERSION="$(node -e 'process.stdout.write(process.versions.node)')"
NODE_MAJOR="${NODE_VERSION%%.*}"
NODE_REST="${NODE_VERSION#*.}"
NODE_MINOR="${NODE_REST%%.*}"
if [ "$NODE_MAJOR" -lt 22 ] || { [ "$NODE_MAJOR" -eq 22 ] && [ "$NODE_MINOR" -lt 6 ]; }; then
    echo "ERROR: node $NODE_VERSION is too old — channel.ts needs 22.6.0 or"
    echo "later for --experimental-strip-types (this repo's floor for it)."
    echo "Install a newer node: https://nodejs.org/"
    exit 1
fi

echo "==> Installing dependencies via npm (node $NODE_VERSION)"
npm install --omit=dev --no-audit --no-fund

CHANNEL_PATH="$SCRIPT_DIR/channel.ts"

cat <<EOF

==> Done.

If you installed this as a Claude Code plugin, the plugin's own \`.mcp.json\`
already declares this server via \${CLAUDE_PLUGIN_ROOT} — nothing below is
needed. These next steps are only for wiring a manual git clone.

Next steps:

1. Register the server in MCP config. For project-level (this repo):

     cat > .mcp.json <<JSON
     {
       "mcpServers": {
         "claude-channel": {
           "command": "node",
           "args": ["--experimental-strip-types", "$CHANNEL_PATH"]
         }
       }
     }
     JSON

   For user-level (always available), add the same block to ~/.claude.json.

2. Launch Claude Code with the channel enabled. During the Channels research
   preview, every channel — including one a plugin declares via its manifest's
   \`channels\` key — needs the development flag, because the flag gate is
   allowlist membership, not manifest declaration: it is bypassed only per
   entry, on every launch, until this plugin (or your org's own
   \`allowedChannelPlugins\`) is on that allowlist.

     claude --dangerously-load-development-channels server:claude-channel

3. In a separate terminal, start watching something:

     ./supertool 'watch:gitlab-mr:21803'

   When the MR's pipeline transitions, Claude sees the event as a
   <channel source="claude-channel" watcher_source="gitlab-mr" id="21803" event="..."> tag.

EOF
