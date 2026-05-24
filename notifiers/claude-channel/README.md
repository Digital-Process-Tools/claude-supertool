# `claude-channel` — MCP channel server (Phase 2 of `watch`)

Bridges the [`watch` preset](../../presets/watch/README.md) UDS event stream
into a running Claude Code session via the
[Channels feature](https://code.claude.com/docs/en/channels.md).

When a Phase 1 poller emits an event, Claude sees it as a
`<channel source="..." id="..." event="...">` tag in its context and can
react immediately.

## Requirements

- Claude Code v2.1.80 or later (Channels research preview)
- [Bun](https://bun.sh) (`curl -fsSL https://bun.sh/install | bash`)
- During the research preview: the `--dangerously-load-development-channels`
  flag (custom channels aren't on the Anthropic allowlist yet)

## Install

```bash
bash notifiers/claude-channel/install.sh
```

Installs `@modelcontextprotocol/sdk` and `@types/bun`. Prints next-step
instructions for `.mcp.json` and launch flags.

## Wire it

Add to your project-level `.mcp.json` (or user-level `~/.claude.json`):

```json
{
  "mcpServers": {
    "claude-channel": {
      "command": "bun",
      "args": ["/abs/path/to/claude-supertool/notifiers/claude-channel/channel.ts"]
    }
  }
}
```

Then launch Claude Code with the channel enabled:

```bash
claude --dangerously-load-development-channels server:claude-channel
```

## How an event reaches Claude

1. You run `./supertool 'watch:gitlab-mr:21803'` (Phase 1) — spawns a poller
2. The poller detects the pipeline failing, writes one NDJSON line to
   `/tmp/supertool-watch.sock`
3. This server reads the line, parses the event, calls
   `mcp.notification({ method: "notifications/claude/channel", ... })`
4. Claude Code injects it into the session as:

   ```
   <channel source="gitlab-mr" id="21803" event="pipeline_failed"
            ts="2026-05-24T19:00:00Z" pipeline_id="139928"
            url="https://gitlab.example.com/.../21803">
   gitlab-mr 21803: pipeline_failed
   feat: do the thing
   https://gitlab.example.com/.../21803
   </channel>
   ```

5. Claude decides what to do based on the server's `instructions` string
   (investigate, notify, fix)

## Configuration

| Env var                  | Default                          | Purpose                                              |
| ------------------------ | -------------------------------- | ---------------------------------------------------- |
| `SUPERTOOL_WATCH_SOCK`   | `/tmp/supertool-watch.sock`      | UDS path. Set the same value on Phase 1 producers.   |

## Security (Phase 2 v1)

- UDS socket bound to a local filesystem path with mode `0600` (owner-only)
- Localhost-only by definition (Unix domain sockets don't traverse the network)
- No sender allowlist beyond filesystem permissions — multi-user machines
  should set `SUPERTOOL_WATCH_SOCK` to a path under `~/.claude/` for
  per-user isolation
- The MCP server validates event shape (source/id/event are strings) before
  emitting to Claude — malformed lines are silently dropped

## Out of scope (future)

- Two-way reply tool (Claude posts back via the channel) — would require
  a per-event ack contract
- Permission relay (approve tool calls remotely) — different feature
- Allowlist of approved senders beyond filesystem ACL
