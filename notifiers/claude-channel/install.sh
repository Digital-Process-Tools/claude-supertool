#!/usr/bin/env bash
# Install the claude-channel MCP server for the supertool 'watch' preset.
#
# - Installs Bun deps (@modelcontextprotocol/sdk + @types/bun)
# - Prints next-step instructions for registering the channel with Claude Code
#
# Usage:
#   bash notifiers/claude-channel/install.sh
#
# Idempotent — safe to re-run.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v bun >/dev/null 2>&1; then
    echo "ERROR: bun is required but not on PATH."
    echo "Install: curl -fsSL https://bun.sh/install | bash"
    exit 1
fi

echo "==> Installing dependencies via bun"
bun install

cat <<'EOF'

==> Done.

Next steps:

1. Register the server in MCP config. For project-level (this repo):

     cat > .mcp.json <<'JSON'
     {
       "mcpServers": {
         "claude-channel": {
           "command": "bun",
           "args": ["$SCRIPT_DIR/channel.ts"]
         }
       }
     }
     JSON

   For user-level (always available), add the same block to ~/.claude.json.

2. Launch Claude Code with the channel enabled. During the Channels research
   preview, custom channels need the development flag:

     claude --dangerously-load-development-channels server:claude-channel

3. In a separate terminal, start watching something:

     ./supertool 'watch:gitlab-mr:21803'

   When the MR's pipeline transitions, Claude sees the event as a
   <channel source="gitlab-mr" id="21803" event="..."> tag in its context.

EOF
