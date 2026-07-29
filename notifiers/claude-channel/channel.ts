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

/**
 * The shape a Phase 1 poller is *meant* to send. Nothing enforces it: lines
 * arrive as JSON from another process, so this is documentation, not a
 * guarantee. Reading a parsed line as a `WatchEvent` is what let a float `ts`
 * reach the wire with the compiler satisfied (#554) — every function that
 * handles a parsed line therefore takes `unknown` and narrows for itself.
 */
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

/**
 * The string form of a value, or null when it has none worth sending.
 *
 * `_meta` is a string map on the receiving end, and the receiver enforces that
 * with a schema — a number reaching it is not a cosmetic mismatch, it throws
 * in the receiver's notification handler (see #554). So nothing leaves this
 * file uncoerced.
 *
 * Scalars coerce honestly. Structure does not: `String({})` is
 * "[object Object]" and `String([1, 2])` is "1,2", and both read downstream as
 * though they were data a poller meant to send. Those are dropped instead.
 */
function asAttr(value: unknown): string | null {
  if (typeof value === "string") return value;
  if (typeof value === "boolean") return String(value);
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return null;
}

function drop(line: string, reason: string): void {
  // Loud on stderr, which `claude --debug` surfaces. Silence here is what made
  // the delivery gap in #554 invisible from inside a session: a dropped event
  // and a delivered one looked identical from every angle available.
  const shown = line.length > 300 ? `${line.slice(0, 300)}…` : line;
  process.stderr.write(`claude-channel: dropped event (${reason}): ${shown}\n`);
}

/**
 * The `_meta` string map for an event, or null when it cannot be routed.
 *
 * Routing keys (`source`, `id`, `event`) are coerced rather than type-checked:
 * poller JSON carries integer ids and epoch timestamps naturally, and a GitLab
 * MR iid arriving as a number is a well-formed event, not a broken one. Being
 * strict there did not protect anything — it dropped the event in silence.
 *
 * What stays strict is presence and shape: a missing routing key, or an object
 * where a scalar belongs, is genuinely malformed and returns null so the caller
 * can drop it with a reason. Coercing that far would turn a broken event into a
 * plausible-looking one, which is worse than losing it.
 */
function buildMeta(raw: unknown): Record<string, string> | null {
  if (!raw || typeof raw !== "object") return null;
  const ev = raw as Partial<WatchEvent>;
  // Claude Code auto-injects `source` from the MCP server name on every event,
  // so we use `watcher_source` for the per-event source (e.g. "gitlab-mr") to
  // avoid the collision. The instructions string tells Claude to route by
  // `watcher_source`.
  const watcherSource = asAttr(ev.source);
  const id = asAttr(ev.id);
  const event = asAttr(ev.event);
  if (watcherSource === null || id === null || event === null) return null;

  const meta: Record<string, string> = { watcher_source: watcherSource, id, event };
  // A `ts` we cannot render is dropped on its own rather than sinking the
  // event: it is context, not routing, and the event is still actionable.
  const ts = asAttr(ev.ts);
  if (ts !== null) meta.ts = ts;
  // Absent stays absent: a poller too old to send the flag has not told us the
  // event was live, and claiming `first_tick="false"` on its behalf would be a
  // confident wrong answer rather than a missing one.
  if (typeof ev.first_tick === "boolean") meta.first_tick = String(ev.first_tick);
  for (const [k, v] of Object.entries(ev.payload || {})) {
    if (!ATTR_KEY_RE.test(k)) continue;
    if (k === "source") continue;  // never let payload overwrite the auto-injected key
    const attr = asAttr(v);
    if (attr === null) continue;
    meta[k] = attr;
  }
  return meta;
}

function buildContent(raw: unknown, meta: Record<string, string>): string {
  const ev = raw as Partial<WatchEvent>;
  // The <channel> tag body — Claude reads this as the event's narrative.
  // Keep it short and route-oriented; payload details live in attributes.
  // Routing values come from `meta` so the body and the attributes cannot
  // disagree about what this event is.
  const payload = (ev.payload ?? {}) as Record<string, unknown>;
  const url = payload.url;
  const title = payload.title;
  const suffix = ev.first_tick === true ? "  (state at watcher start)" : "";
  const lines: string[] = [`${meta.watcher_source} ${meta.id}: ${meta.event}${suffix}`];
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
      let ev: unknown;
      try {
        ev = JSON.parse(line);
      } catch {
        drop(line, "not valid JSON");
        continue;
      }
      // Everything between here and the send is synchronous, and a synchronous
      // throw inside a Node `data` handler is not a lost event — it is an
      // uncaught exception that takes this whole process down, socket and all.
      // The `.catch()` below covers only the async send, which is a different
      // failure. One malformed event must cost one event.
      try {
        const meta = buildMeta(ev);
        if (meta === null) {
          drop(line, "missing or non-scalar source/id/event");
          continue;
        }
        mcp
          .notification({
            method: "notifications/claude/channel",
            params: { content: buildContent(ev, meta), meta },
          })
          .catch((err) => {
            // Surface to stderr so `claude --debug` picks it up. Common causes:
            // Claude Code transport closed, channel not registered, JSON-RPC busy.
            process.stderr.write(`claude-channel: notify failed: ${String(err)}\n`);
          });
      } catch (err) {
        drop(line, `handler threw: ${String(err)}`);
      }
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
