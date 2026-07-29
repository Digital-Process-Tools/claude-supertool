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
   <channel source="claude-channel" watcher_source="gitlab-mr" id="21803"
            event="pipeline_failed" ts="2026-05-24T19:00:00Z"
            pipeline_id="139928" url="https://gitlab.example.com/.../21803">
   gitlab-mr 21803: pipeline_failed
   feat: do the thing
   https://gitlab.example.com/.../21803
   </channel>
   ```

   Note: `source="claude-channel"` is auto-injected by Claude Code from the
   MCP server name. The per-event source (which Phase 1 source emitted the
   event) lands in `watcher_source`. Route Claude's logic on `watcher_source`
   + `event`.

5. Claude decides what to do based on the server's `instructions` string
   (investigate, notify, fix)

## Event contract

An event line is NDJSON with `source`, `id` and `event` (routing), plus
optional `ts`, `first_tick` and a flat `payload` object.

Scalars are coerced, not type-checked: `"ts": 1785362036.7` and `"id": 21803`
are well-formed, because poller JSON carries epoch numbers and integer ids
naturally and `_meta` wanting strings is this server's mismatch to absorb.
Every value that reaches Claude is a string.

What is *not* coerced is structure. A payload value that is an object or an
array has no honest string form — `String({})` is `"[object Object]"` — so the
attribute is omitted and the rest of the event still goes through. A missing or
structured `source`/`id`/`event` drops the whole event, since it cannot be
routed. Every drop writes one line to stderr, visible under `claude --debug`.

A malformed event costs one event. It never affects the connection
([#554](https://github.com/Digital-Process-Tools/claude-supertool/issues/554)).

## Start-up and socket ownership

**One server owns the watch socket. A second one refuses to start rather than
take it.**

The socket path is a *name*; a connection is to the *inode* behind it. Removing
the name does not disturb the server holding the inode — it goes on
`listen()`ing, its watchers still look healthy, and it never receives another
event. There is no error on either side. That is why the start-up rules below
are rules and not preferences
([#550](https://github.com/Digital-Process-Tools/claude-supertool/issues/550)).

On start-up the server tries to `bind()` first, and only asks questions if the
kernel refuses:

| What it finds at `SUPERTOOL_WATCH_SOCK`             | What it does                                              |
| --------------------------------------------------- | --------------------------------------------------------- |
| Nothing                                             | Binds. The ordinary case.                                  |
| A socket file, nothing listening (crashed server)   | Removes it, binds, notes the cleanup on stderr.            |
| A socket file with a **live** server                | **Refuses to start**, exit code `3`, reason on stderr.     |
| A path that exists and is not a socket              | Refuses. A mistyped path costs a start-up error, not the file. |
| Anything it cannot determine (`EACCES`, a timeout)  | Refuses. "I could not tell" is never rounded to "nobody's home". |

Liveness is decided by connecting, not by an errno: node reports
`ECONNREFUSED` for a stale socket file and bun reports `ENOENT`, and this
server runs under either. It also asks more than once — on BSD/macOS a
connection to a listener whose *backlog is full* is refused with the same code
as one with no listener at all, so a single refusal is not proof.

### If it refuses

The refusal is deliberate: a second radar quietly replacing the first is the
failure this is preventing. Two ways forward:

- Stop the other session, or
- give this session a channel of its own by pointing it at an unused path —
  `SUPERTOOL_WATCH_SOCK=/tmp/supertool-watch-$$.sock`, set **both** here and on
  every Phase 1 poller that should feed it. Pollers write to the path they are
  given; a session and its watchers simply have to agree on one.

Concurrent sessions sharing a *single* stream of events would need a broker
that fans one event out to every connected server. That is not this — see
"Out of scope".

### When a session ends

The server exits and removes its socket when stdio closes, so restarting a
session needs no cleanup. Without this, a leaked server would hold the path
after its session was gone and the refusal above would turn into a permanent
outage — trading a silent bug for a loud one, which is not an improvement.
#550 observed two such orphans, parents long dead, still owning the path.

## Configuration

| Env var                  | Default                          | Purpose                                              |
| ------------------------ | -------------------------------- | ---------------------------------------------------- |
| `SUPERTOOL_WATCH_SOCK`   | `/tmp/supertool-watch.sock`      | UDS path. Set the same value on Phase 1 producers. Also the way to run a second session's channel alongside a first — see "Start-up and socket ownership". |

Exit codes: `3` means another server owns the watch socket (or its state could
not be established) and this one declined to take it.

## Security (Phase 2 v1)

- UDS socket bound to a local filesystem path with mode `0600` (owner-only)
- Localhost-only by definition (Unix domain sockets don't traverse the network)
- No sender allowlist beyond filesystem permissions — multi-user machines
  should set `SUPERTOOL_WATCH_SOCK` to a path under `~/.claude/` for
  per-user isolation
- The socket carries events only. It has no control verbs, so the worst a
  process that can write to it can do is inject a false event — it cannot ask
  the server to stand down. That is why a newcomer refuses to start rather than
  evicting the incumbent over the same channel: eviction-on-request would make
  "silently disable the radar" reachable by anything that can already write to
  it
- The MCP server checks event shape before emitting to Claude, and drops what
  it cannot route (see "Event contract" above) — rejected lines are reported on
  stderr, never sent on

## Out of scope (future)

- Two-way reply tool (Claude posts back via the channel) — would require
  a per-event ack contract
- Permission relay (approve tool calls remotely) — different feature
- Allowlist of approved senders beyond filesystem ACL
