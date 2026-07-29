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
 *
 * Socket ownership:
 *   One server owns the socket. A second refuses to start (exit 3) rather than
 *   unlink a live incumbent, which would leave it listening on an unnamed
 *   inode — alive, healthy-looking and unreachable (#550). Use
 *   SUPERTOOL_WATCH_SOCK to give a second session a channel of its own.
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

// Ensure the socket's parent dir exists so non-default SUPERTOOL_WATCH_SOCK
// paths (e.g. ~/.claude/supertool-watch.sock) don't fail with a cryptic ENOENT.
try {
  const parent = SOCK_PATH.includes("/") ? SOCK_PATH.slice(0, SOCK_PATH.lastIndexOf("/")) : "";
  if (parent) fs.mkdirSync(parent, { recursive: true });
} catch (err) {
  process.stderr.write(`claude-channel: could not ensure parent dir: ${String(err)}\n`);
}

/**
 * Exit code for "someone else owns the watch socket". Distinct from the
 * launcher shim's 1 (channel.ts not found / no runtime) so `claude mcp list`
 * tells the two apart.
 */
const EXIT_SOCKET_CONFLICT = 3;

/**
 * True once this process has bound SOCK_PATH. Nothing here may unlink a path
 * it did not bind — that single missing condition is the whole of #550.
 */
let bound = false;

function refuse(reason: string): never {
  process.stderr.write(
    `claude-channel: refusing to start — ${reason}\n` +
      `  Watch socket: ${SOCK_PATH}\n` +
      `  Taking it would leave the other server listening on an unnamed inode:\n` +
      `  alive, watchers all green, and unreachable — a dead radar that reads as\n` +
      `  a healthy one (#550). One session with a channel beats two half-blind.\n` +
      `  To give this session its own: set SUPERTOOL_WATCH_SOCK to an unused path,\n` +
      `  here and on every poller that feeds it. Or stop the other session.\n`,
  );
  process.exit(EXIT_SOCKET_CONFLICT);
}

type Probe = "live" | "vacant" | { code: string };

/**
 * Whether anything is listening on `path`.
 *
 * A UDS `connect()` is completed by the kernel from the listen backlog, so it
 * succeeds even against a server too wedged to `accept()`. That is the answer
 * we want: an inode with a listener is not ours to unlink, however unhealthy
 * it looks from outside.
 *
 * "Vacant" is deliberately *not* decided on a single errno. Against the same
 * stale socket file, node reports `ECONNREFUSED` and bun reports `ENOENT`, and
 * `claude-channel.sh` will launch us under either. Both mean the same thing —
 * nobody answered — so both map to `vacant`, and whether a file is sitting
 * there is then settled by `lstat`, not by guessing which runtime we are.
 * Anything else (`EACCES`, a timeout, someone else's 0600 socket) is not
 * evidence of vacancy and stays an error: "I could not tell" must never be
 * rounded down to "nobody is home".
 */
function probe(path: string): Promise<Probe> {
  return new Promise((resolve) => {
    const sock = net.connect(path);
    let settled = false;
    const done = (result: Probe): void => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      sock.destroy();
      resolve(result);
    };
    // A connect that neither completes nor refuses tells us nothing, and
    // "nothing" must not be read as "vacant".
    const timer = setTimeout(() => done({ code: "ETIMEDOUT" }), 2000);
    timer.unref?.();
    sock.on("connect", () => done("live"));
    sock.on("error", (err) => {
      const code = (err as NodeJS.ErrnoException).code;
      if (code === "ECONNREFUSED" || code === "ENOENT") return done("vacant");
      done({ code: code || String(err) });
    });
  });
}

/**
 * `probe`, repeated, because one refused connect is not proof of vacancy.
 *
 * On BSD/macOS a `connect()` to a listener whose backlog is full is refused
 * with `ECONNREFUSED` — the same answer as a socket with no listener at all.
 * So a live-but-saturated server can read as vacant on a single ask, and the
 * consequence of believing that is #550 all over again, just rarer.
 *
 * A healthy `net.createServer` accepts immediately and never holds a full
 * backlog, so a real incumbent answers "live" on the first or second try. Only
 * a unanimous set of refusals counts as vacant. Any single "live" wins.
 */
async function probeRepeatedly(path: string, tries = 3): Promise<Probe> {
  let last: Probe = "vacant";
  for (let attempt = 0; attempt < tries; attempt++) {
    if (attempt > 0) await new Promise((resolve) => setTimeout(resolve, 100));
    last = await probe(path);
    if (last !== "vacant") return last;
  }
  return last;
}

/** One `listen()` attempt: null when bound, otherwise the bind error. */
function tryListen(srv: net.Server): Promise<NodeJS.ErrnoException | null> {
  return new Promise((resolve) => {
    const onError = (err: NodeJS.ErrnoException): void => {
      srv.removeListener("listening", onListening);
      resolve(err);
    };
    const onListening = (): void => {
      srv.removeListener("error", onError);
      resolve(null);
    };
    srv.once("error", onError);
    srv.once("listening", onListening);
    srv.listen(SOCK_PATH);
  });
}

/**
 * Bind SOCK_PATH, or exit non-zero saying why not.
 *
 * Bind first, ask questions second. Unlinking up front could never fail, which
 * is precisely how it evicted live servers in silence: `bind()` never saw
 * `EADDRINUSE` because the caller had just freed the name itself. Here the
 * path is removed only after `bind(2)` has refused it *and* a `connect()` has
 * proved nothing is listening — so recovery from a crashed server, the case
 * the original unlink existed for, still works.
 */
async function bindOrRefuse(srv: net.Server): Promise<void> {
  const first = await tryListen(srv);
  if (first === null) {
    bound = true;
    return;
  }
  if (first.code !== "EADDRINUSE") refuse(`bind failed: ${first.code || String(first)}`);

  const state = await probeRepeatedly(SOCK_PATH);
  if (state === "live") refuse("another claude-channel server is listening there");
  if (typeof state !== "string") {
    refuse(`cannot tell whether the socket is live (connect: ${state.code})`);
  }

  // Nobody answered. If something is still at the path it is the leftover of a
  // crashed server — the case the original unconditional unlink existed for,
  // and the one this must not break.
  let leftover: fs.Stats | null = null;
  try {
    leftover = fs.lstatSync(SOCK_PATH);
  } catch {
    // Vanished between probe and stat; the retry below just binds.
  }
  if (leftover !== null) {
    // Only ever delete a socket. A misconfigured SUPERTOOL_WATCH_SOCK pointing
    // at a real file should cost a start-up error, not the file.
    if (!leftover.isSocket()) {
      refuse(`${SOCK_PATH} exists and is not a socket — refusing to delete it`);
    }
    try {
      fs.unlinkSync(SOCK_PATH);
    } catch (err) {
      refuse(`stale socket could not be removed: ${String(err)}`);
    }
    process.stderr.write(
      `claude-channel: cleared a stale watch socket at ${SOCK_PATH} — nothing was listening\n`,
    );
  }

  // Losing this second race means someone bound between our probe and here.
  // They are live and we are not, so the same rule applies: step aside.
  const second = await tryListen(srv);
  if (second !== null) {
    refuse(`lost the socket to another server while clearing it: ${second.code || String(second)}`);
  }
  bound = true;
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

await bindOrRefuse(server);

try {
  fs.chmodSync(SOCK_PATH, 0o600);
} catch {
  // Permission tightening is advisory; UDS is already localhost-only.
}

// The socket is settled before the MCP handshake, so a server that refuses
// never registers as a healthy channel: Claude Code sees it die during
// start-up and `claude mcp list` reports a failure, instead of a green entry
// that would never deliver anything.
await mcp.connect(new StdioServerTransport());

server.on("error", (err) => {
  // Surface bind errors via stderr so `claude --debug` can show them.
  process.stderr.write(`claude-channel: socket error: ${String(err)}\n`);
});

// Clean shutdown — release the socket on SIGINT/SIGTERM, or when the session
// that launched us goes away.
const cleanup = (): void => {
  try {
    server.close();
  } catch {}
  // Only ever remove a path this process bound. A refusing server exits before
  // `bound` is set and leaves the incumbent's socket exactly where it found it.
  if (bound) {
    try {
      fs.unlinkSync(SOCK_PATH);
    } catch {}
  }
  process.exit(0);
};
process.on("SIGINT", cleanup);
process.on("SIGTERM", cleanup);

// Stdio EOF means Claude Code is gone: there is nobody left to deliver to, and
// holding the watch socket would deny it to every future session. #550 found
// two such orphans, parent sessions long dead, still owning the path — which
// is what would turn "refuse when someone is listening" from a safeguard into
// a permanent outage. Let go instead.
process.stdin.on("end", cleanup);
process.stdin.on("close", cleanup);
