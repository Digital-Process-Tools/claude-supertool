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

/** Exit code for a cap override that is not a usable number. */
const EXIT_BAD_CAP = 4;

/**
 * A size cap from the environment, or the default.
 *
 * An unreadable override exits rather than falling back. Quietly substituting
 * the default would leave an operator who set `…_ATTR_MAX=2O48` believing a
 * limit is in force that isn't, which is the failure this whole file is about
 * wearing an operations hat.
 */
function capFromEnv(name: string, fallback: number): number {
  const raw = process.env[name];
  if (raw === undefined || raw.trim() === "") return fallback;
  const n = Number(raw);
  if (!Number.isInteger(n) || n < 1) {
    process.stderr.write(
      `claude-channel: refusing to start — ${name}=${raw} is not a positive integer\\n`,
    );
    process.exit(EXIT_BAD_CAP);
  }
  return n;
}

/**
 * How much of the session's context window one event may spend (#605).
 *
 * The window is the scarce resource here: a Claude Code session is ~200K
 * tokens, and at ~4 chars per token that is ~800 KB of text for the whole
 * conversation. One measured event carrying two 400 KB strings left this
 * server at 1,600,261 bytes — twice the entire window, in one notification,
 * for one MR going red.
 *
 * The numbers are set from what real pollers actually send, not from what
 * felt safe. Across ten live watchers the largest complete payload was 488
 * characters and its longest single value 117 (an MR title); `gitlab-mr`
 * already bounds its one unbounded field to five job names (`FAILED_JOBS_MAX`).
 * So:
 *
 * - `ATTR_MAX_CHARS` 2048 is ~17x the longest value ever observed. Nothing at
 *   that size is a title or a URL; it is a log paste or a field somebody
 *   widened, and it is the shape #605 reported.
 * - `EVENT_MAX_CHARS` 8192 is ~17x the largest complete payload observed, and
 *   ~1% of the context window — the budget for one event, chosen so that a
 *   busy radar day is spent on events rather than on any single one of them.
 *   It is the *necessary* axis: 2,000 well-formed 200-char attributes measured
 *   425,123 bytes delivered and no per-attribute limit touches them.
 *
 * `LINE_MAX_CHARS` is a different budget and deliberately far larger. It is
 * what may be *assembled* before a newline arrives, not what may be delivered:
 * an oversized-but-parseable event has to reach `buildMeta` in order to be
 * clamped and disclosed, so refusing to parse at the delivery cap would turn
 * every disclosure into a silent loss. Above 1 MB there is no event anyone
 * meant to send, and holding the bytes costs real memory (see the read loop).
 *
 * All three are overridable for an operator who knows their traffic, matching
 * `GL_JOB_RAW_MAX_LINES`.
 */
const ATTR_MAX_CHARS = capFromEnv("SUPERTOOL_CHANNEL_ATTR_MAX", 2048);
const EVENT_MAX_CHARS = capFromEnv("SUPERTOOL_CHANNEL_EVENT_MAX", 8192);
const LINE_MAX_CHARS = capFromEnv("SUPERTOOL_CHANNEL_LINE_MAX", 1_048_576);

/**
 * Keys that identify the event rather than describe it.
 *
 * These are never withheld to save space. They are what makes an event
 * actionable at all — "gitlab-mr 33173: pipeline_failed" is the entire product
 * of this bridge — and they are tiny, so they are never the reason an event is
 * over budget. An event whose *routing* is oversized has nothing worth
 * delivering and is dropped instead (see `clampMeta`).
 */
const ROUTING_KEYS = new Set(["watcher_source", "id", "event", "ts", "first_tick"]);

/**
 * Names this bridge writes itself, which a payload key therefore may not claim.
 *
 * `ROUTING_KEYS` is spread in rather than re-listed on purpose (#609). The
 * argument for namespacing the payload instead was that a guard has to be
 * remembered the next time a routing field is added — and #608 had just added
 * two, so that was not hypothetical. Deriving the guard from the same constant
 * `clampMeta` iterates removes the "next time": the edit that adds a routing
 * field protects it in the same breath.
 *
 * The rest are not routing but are still ours to write:
 * - `source` is auto-injected by Claude Code from the MCP server name.
 * - `clamped` and `collided` are disclosures. A producer-writable disclosure is
 *   worse than no disclosure: an event that lost nothing could announce that it
 *   had, on the one surface that exists to be believed.
 * - `__proto__` cannot become an attribute at all. `meta["__proto__"] = "x"` on
 *   an object literal runs the inherited setter and creates no own property, so
 *   the key would leave no trace anywhere — a silent loss, which is the defect
 *   class this file keeps refusing rather than a curiosity.
 */
const RESERVED_KEYS = new Set([
  ...ROUTING_KEYS,
  "source",
  "clamped",
  "collided",
  "__proto__",
]);

/** The reserved names, for a disclosure a poller author can act on. */
const RESERVED_LIST = [...RESERVED_KEYS].join(", ");

/**
 * One attribute that did not survive, and how big it really was.
 *
 * Shared by both reductions — over the size cap (#605) and losing a name to a
 * reserved key (#609) — because they are the same fact to a reader: something
 * the poller sent is not here, and this is what and how much.
 */
interface Withheld {
  key: string;
  chars: number;
}

/** How many withheld attributes are named before the list is summarised. */
const WITHHELD_NAMED_MAX = 5;

/**
 * A routable `_meta` map, plus the payload keys that were refused in building
 * it. The refusals travel with the map rather than being applied to it here:
 * the disclosure must survive `clampMeta`, and an attribute added before the
 * clamp is an attribute the clamp may withhold.
 */
interface BuiltMeta {
  meta: Record<string, string>;
  collided: Withheld[];
}

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
function buildMeta(raw: unknown): BuiltMeta | null {
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
  // The merge that follows used to overwrite everything set above, guarding
  // only `source`. Measured on 235b377: an event announced as
  // `gitlab-mr 33173: pipeline_failed` was delivered as
  // `not-gitlab 11111: pipeline_succeeded`, body and attributes agreeing, a red
  // pipeline reading as a green one, nothing on stderr. The one set of values
  // the design treats as load-bearing was the one set a producer could replace
  // by accident.
  const collided: Withheld[] = [];
  for (const [k, v] of Object.entries(ev.payload || {})) {
    if (!ATTR_KEY_RE.test(k)) continue;
    // Coerced before the guard so the disclosure can state the real size, and
    // so a structured value stays `asAttr`'s business rather than being
    // reported as a name collision it also isn't.
    const attr = asAttr(v);
    if (attr === null) continue;
    if (RESERVED_KEYS.has(k)) {
      collided.push({ key: k, chars: attr.length });
      continue;
    }
    meta[k] = attr;
  }
  return { meta, collided };
}

/**
 * The sentence a collided event carries about the keys it did not deliver.
 *
 * Built the way `describeWithheld` is built, deliberately: #608 established one
 * vocabulary for "the poller sent this and you are not seeing it", and a second
 * one would make a reader learn two. It names the reserved set as well as the
 * losing keys, because the producer's fix is to rename the field and that is
 * not guessable from the key alone.
 */
function describeCollided(collided: Withheld[]): string {
  const noun = collided.length === 1 ? "payload key" : "payload keys";
  const named = collided
    .slice(0, WITHHELD_NAMED_MAX)
    .map((c) => `${c.key} (${c.chars} chars)`);
  if (collided.length > named.length) {
    named.push(`+${collided.length - named.length} more`);
  }
  const full =
    `${collided.length} ${noun} ignored — ${named.join(", ")}; ` +
    `reserved: ${RESERVED_LIST}`;
  if (full.length <= ATTR_MAX_CHARS) return full;
  return `${collided.length} ${noun} ignored; reserved: ${RESERVED_LIST}`;
}

/**
 * The sentence a clamped event carries about its own clamping.
 *
 * It names the attributes and their *real* sizes, because "title was too big"
 * is actionable and "something was too big" is not. The list is bounded the
 * way `gitlab-mr`'s `observed_failed_jobs` is bounded — a `+N more` marker
 * inside the value, so a surface rendering this one attribute cannot read the
 * first five as the whole story.
 *
 * The short form is the fallback for the case where the *key names* are
 * themselves enormous: a disclosure that broke the cap it is announcing would
 * be an easy joke and a real bug.
 */
function describeWithheld(withheld: Withheld[]): string {
  const limits =
    `limits: ${ATTR_MAX_CHARS} chars/attribute, ${EVENT_MAX_CHARS} chars/event`;
  const noun = withheld.length === 1 ? "attribute" : "attributes";
  const named = withheld
    .slice(0, WITHHELD_NAMED_MAX)
    .map((w) => `${w.key} (${w.chars} chars)`);
  if (withheld.length > named.length) {
    named.push(`+${withheld.length - named.length} more`);
  }
  const full = `${withheld.length} ${noun} withheld — ${named.join(", ")}; ${limits}`;
  if (full.length <= ATTR_MAX_CHARS) return full;
  return `${withheld.length} ${noun} withheld; ${limits}`;
}

/**
 * Total characters `meta` costs the session — keys included, since every key
 * is rendered as an attribute name in the `<channel>` tag.
 */
function metaChars(meta: Record<string, string>): number {
  let total = 0;
  for (const [k, v] of Object.entries(meta)) total += k.length + v.length;
  return total;
}

/**
 * Bring `meta` inside the size caps, reporting what that cost. `null` means
 * the event cannot be delivered at all.
 *
 * **Attributes are withheld whole, never shortened.** A 400 KB title has no
 * honest short form, in exactly the sense `asAttr` already uses for structure:
 * `String({})` is "[object Object]" and reads downstream as data somebody meant
 * to send, and the first 2,048 characters of a title read downstream as the
 * title. Truncating converts "your event was too big" into "the tool quietly
 * showed you something else", so nothing here truncates — the attribute goes,
 * and `clamped` says it went.
 *
 * **The event is not dropped for it.** Dropping would lose the routing signal
 * — that MR 33173 went red — over bytes that were never the point, and a
 * silently absent event is the failure #554 was filed about. The one case with
 * no good half is a *routing* key over the cap: withholding it delivers a
 * notification that says nothing about what happened, so that returns `null`
 * and the caller drops it through the existing loud path.
 *
 * The per-event pass removes largest-first, which reaches the budget by
 * withholding the fewest attributes — the reader keeps the most distinct
 * facts, rather than the most bytes.
 */
function clampMeta(meta: Record<string, string>): Withheld[] | null {
  for (const key of ROUTING_KEYS) {
    const value = meta[key];
    if (value !== undefined && value.length > ATTR_MAX_CHARS) return null;
  }

  const withheld: Withheld[] = [];
  for (const [k, v] of Object.entries(meta)) {
    if (ROUTING_KEYS.has(k)) continue;
    if (v.length > ATTR_MAX_CHARS) {
      withheld.push({ key: k, chars: v.length });
      delete meta[k];
    }
  }

  let size = metaChars(meta);
  if (size > EVENT_MAX_CHARS) {
    const byCost = Object.keys(meta)
      .filter((k) => !ROUTING_KEYS.has(k))
      .sort((a, b) => meta[b].length + b.length - (meta[a].length + a.length));
    for (const k of byCost) {
      if (size <= EVENT_MAX_CHARS) break;
      size -= k.length + meta[k].length;
      withheld.push({ key: k, chars: meta[k].length });
      delete meta[k];
    }
  }
  // Routing alone can exceed the budget only if several routing values sit
  // just under the per-attribute cap, and it is still delivered: routing is
  // the thing the event exists to carry. The alternative is a silent loss.
  return withheld;
}

function buildContent(
  meta: Record<string, string>,
  withheld: Withheld[],
  collided: Withheld[],
): string {
  // The <channel> tag body — Claude reads this as the event's narrative.
  // Keep it short and route-oriented; payload details live in attributes.
  //
  // Every value here comes from `meta`, and since #609 there is no raw event in
  // scope to reach for instead. Reading the event a second time is what made the
  // two surfaces disagree, twice: before #605 it put the very bytes the
  // attributes had just withheld back into the narrative, all 1.6 MB of them,
  // and until #609 it read `first_tick` from the raw event while the attribute
  // came from `meta` — one event, one fact, two answers.
  const suffix = meta.first_tick === "true" ? "  (state at watcher start)" : "";
  const lines: string[] = [`${meta.watcher_source} ${meta.id}: ${meta.event}${suffix}`];
  if (meta.title) lines.push(meta.title);
  if (meta.url) lines.push(meta.url);
  // The disclosure goes in the body as well as in an attribute. The body is
  // what Claude reads as prose, and an event that was reduced has to be
  // distinguishable from a complete one *from the event itself* — not by
  // anyone thinking to go and compare it against the source.
  if (withheld.length > 0) lines.push(`[claude-channel] ${describeWithheld(withheld)}`);
  if (collided.length > 0) lines.push(`[claude-channel] ${describeCollided(collided)}`);
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
  // True while we are throwing away the tail of a line that was refused for
  // length. Resuming mid-line would hand `JSON.parse` a fragment, and a
  // fragment that happens to parse is an event nobody sent.
  let resyncing = false;
  conn.setEncoding("utf-8");
  conn.on("data", (chunk: string) => {
    if (resyncing) {
      const cut = chunk.indexOf("\n");
      if (cut === -1) return;  // still inside the refused line
      resyncing = false;
      chunk = chunk.slice(cut + 1);
    }
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
        const built = buildMeta(ev);
        if (built === null) {
          drop(line, "missing or non-scalar source/id/event");
          continue;
        }
        const { meta, collided } = built;
        const withheld = clampMeta(meta);
        if (withheld === null) {
          drop(line, `routing key over ${ATTR_MAX_CHARS} chars — nothing routable to deliver`);
          continue;
        }
        // Added after the clamp, and deliberately not counted against it. An
        // event that went one line over budget in order to say it was clamped
        // is correct; one that stayed under by staying quiet is the defect.
        if (withheld.length > 0) meta.clamped = describeWithheld(withheld);
        if (collided.length > 0) meta.collided = describeCollided(collided);
        mcp
          .notification({
            method: "notifications/claude/channel",
            params: { content: buildContent(meta, withheld, collided), meta },
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
    // Everything above consumed the complete lines; what is left is a partial
    // one. Nothing bounded it until #605, and an NDJSON stream that never
    // sends a newline is not hypothetical — a poller killed mid-write does it.
    // Measured on 4da713f: 50 MB with no newline took this server from 74 MB
    // to 770 MB RSS, super-linearly (each chunk rescans the whole buffer),
    // delivering nothing, logging nothing and staying green from every angle
    // a session can check. That is #554's invisible failure with a memory leak
    // attached, so it is refused out loud and the connection resyncs.
    if (buf.length > LINE_MAX_CHARS) {
      drop(
        buf,
        `line exceeded ${LINE_MAX_CHARS} chars with no newline (${buf.length} buffered)` +
          " — discarding to the next newline",
      );
      buf = "";
      resyncing = true;
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
