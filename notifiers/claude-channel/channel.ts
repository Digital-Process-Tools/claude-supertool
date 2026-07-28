#!/usr/bin/env bun
/**
 * claude-channel — MCP channel server bridging watch-preset events to Claude Code.
 *
 * Phase 1 pollers write NDJSON to the UDS socket at /tmp/supertool-watch.sock.
 * This server is the consumer: it binds that socket, reads each event line,
 * and pushes it into Claude Code via `notifications/claude/channel`.
 *
 * Claude sees each event as a `<channel source="..." id="..." event="...">`
 * tag in its context.
 *
 * Launch:
 *   claude --dangerously-load-development-channels server:claude-channel
 *
 * (The `--dangerously-load-development-channels` flag is required during the
 * Channels research preview until the plugin lands on the Anthropic allowlist.)
 *
 * Auth model — Phase 2 v1:
 *   Localhost UDS file with mode 0600 (owner-only). Any process running as
 *   the same user can connect. Multi-user machines should use a per-user
 *   override path (set SUPERTOOL_WATCH_SOCK env var on both producers and
 *   this server).
 */
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import * as fs from "node:fs";
import * as net from "node:net";

const SOCK_PATH = process.env.SUPERTOOL_WATCH_SOCK || "/tmp/supertool-watch.sock";

interface WatchEvent {
  ts: string;
  source: string;
  id: string;
  event: string;
  payload: Record<string, unknown>;
  /** True when the watcher emitted this on its first poll — the state it
   *  found, not a change it observed. Absent on records from a poller that
   *  predates the field, which is "unknown" and not "false". */
  first_tick?: boolean;
}

// Identifier keys (letters/digits/underscores) become <channel> tag attributes.
// Hyphens and other characters are silently dropped by Claude Code per the
// notification protocol, so we sanitize keys here too.
const ATTR_KEY_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

function buildMeta(ev: WatchEvent): Record<string, string> {
  // Claude Code auto-injects `source` from the MCP server name on every event,
  // so we use `watcher_source` for the per-event source (e.g. "gitlab-mr") to
  // avoid the collision. The instructions string tells Claude to route by
  // `watcher_source`.
  const meta: Record<string, string> = {
    watcher_source: ev.source,
    id: ev.id,
    event: ev.event,
    ts: ev.ts,
  };
  // Absent stays absent: a poller too old to send the flag has not told us the
  // event was live, and claiming `first_tick="false"` on its behalf would be a
  // confident wrong answer rather than a missing one.
  if (typeof ev.first_tick === "boolean") meta.first_tick = String(ev.first_tick);
  for (const [k, v] of Object.entries(ev.payload || {})) {
    if (!ATTR_KEY_RE.test(k)) continue;
    if (k === "source") continue;  // never let payload overwrite the auto-injected key
    if (v === null || v === undefined) continue;
    meta[k] = String(v);
  }
  return meta;
}

function buildContent(ev: WatchEvent): string {
  // The <channel> tag body — Claude reads this as the event's narrative.
  // Keep it short and route-oriented; payload details live in attributes.
  const url = (ev.payload as Record<string, unknown>)?.url;
  const title = (ev.payload as Record<string, unknown>)?.title;
  const suffix = ev.first_tick === true ? "  (state at watcher start)" : "";
  const lines: string[] = [`${ev.source} ${ev.id}: ${ev.event}${suffix}`];
  if (typeof title === "string" && title) lines.push(title);
  if (typeof url === "string" && url) lines.push(url);
  return lines.join("\n");
}

const mcp = new Server(
  { name: "claude-channel", version: "0.0.1" },
  {
    capabilities: {
      experimental: { "claude/channel": {} },
    },
    instructions:
      "Events from the supertool 'watch' preset arrive as " +
      "<channel source=\"claude-channel\" watcher_source=\"<source>\" id=\"<watcher-id>\" event=\"<event-key>\" ...>. " +
      "Route by `watcher_source` (e.g. \"gitlab-mr\") and `event` (e.g. \"pipeline_failed\"). " +
      "They are one-way (no reply expected). Investigate via the matching supertool op " +
      "(e.g. ./supertool 'gl-mr:<id>'), post a summary, or notify the human. " +
      "Status-change is the signal; consecutive events for the same watcher_source/id " +
      "supersede each other. " +
      "`first_tick=\"true\"` means the watcher emitted this on its first poll: it is the " +
      "current state it found on startup, which may be days old, not something that just " +
      "changed. Report it as context, not as news. The attribute being absent means the " +
      "poller predates the field — unknown, not false.",
  },
);

await mcp.connect(new StdioServerTransport());

// Ensure the socket's parent dir exists so non-default SUPERTOOL_WATCH_SOCK
// paths (e.g. ~/.claude/supertool-watch.sock) don't fail with a cryptic ENOENT.
try {
  const parent = SOCK_PATH.includes("/") ? SOCK_PATH.slice(0, SOCK_PATH.lastIndexOf("/")) : "";
  if (parent) fs.mkdirSync(parent, { recursive: true });
} catch (err) {
  process.stderr.write(`claude-channel: could not ensure parent dir: ${String(err)}\n`);
}

// Bind the UDS socket. Unlink stale file from a previous crash first.
try {
  fs.unlinkSync(SOCK_PATH);
} catch {
  // ENOENT — fine, nothing to clean
}

const server = net.createServer((conn) => {
  let buf = "";
  conn.setEncoding("utf-8");
  conn.on("data", (chunk: string) => {
    buf += chunk;
    let nl = buf.indexOf("\n");
    while (nl !== -1) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      nl = buf.indexOf("\n");
      if (!line) continue;
      let ev: WatchEvent;
      try {
        ev = JSON.parse(line) as WatchEvent;
      } catch {
        continue;
      }
      if (!ev || typeof ev.source !== "string" || typeof ev.id !== "string" || typeof ev.event !== "string") {
        continue;
      }
      mcp
        .notification({
          method: "notifications/claude/channel",
          params: { content: buildContent(ev), meta: buildMeta(ev) },
        })
        .catch((err) => {
          // Surface to stderr so `claude --debug` picks it up. Common causes:
          // Claude Code transport closed, channel not registered, JSON-RPC busy.
          process.stderr.write(`claude-channel: notify failed: ${String(err)}\n`);
        });
    }
  });
  conn.on("error", () => {
    // Best-effort: a producer hang/timeout shouldn't kill the server.
  });
});

server.listen(SOCK_PATH, () => {
  try {
    fs.chmodSync(SOCK_PATH, 0o600);
  } catch {
    // Permission tightening is advisory; UDS is already localhost-only.
  }
});

server.on("error", (err) => {
  // Surface bind errors via stderr so `claude --debug` can show them.
  process.stderr.write(`claude-channel: socket error: ${String(err)}\n`);
});

// Clean shutdown — remove the socket file on SIGINT/SIGTERM.
const cleanup = () => {
  try {
    server.close();
  } catch {}
  try {
    fs.unlinkSync(SOCK_PATH);
  } catch {}
  process.exit(0);
};
process.on("SIGINT", cleanup);
process.on("SIGTERM", cleanup);
