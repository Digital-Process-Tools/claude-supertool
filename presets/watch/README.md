# `watch` preset

Background pollers for external sources (GitLab MRs, jobs, calendar, …) that
emit events when state changes. Reusable foundation — a Phase 2 channel
consumer will plug into the same UDS socket to push events directly into
Claude Code.

## Ops

```
watch:SOURCE:ID[:only=event1,event2]    spawn poller (fire-and-forget)
unwatch:SOURCE:ID                       kill poller, remove PID file
watches                                 list active pollers (table), and count the ones
                                        on other channels it may not touch (#1881)
channel:health                          is the bridge to the session actually delivering?
channel:probe                           put one synthetic event through the path, now, and say what moved
radar                                   reconcile registered tiers against live truth, report
radar:--state                           the same tiers, read-only — spawns nothing
```

Example:

```bash
./supertool 'watch:gitlab-mr:21803'                 # all events
./supertool 'watch:gitlab-mr:21803:only=pipeline_failed,merged'
./supertool 'watch:gl-pipeline:151111'              # poll a CI pipeline to completion
./supertool 'watch:gh-run:18234567890'              # poll a GitHub Actions run to completion
./supertool 'watches'
./supertool 'unwatch:gitlab-mr:21803'
./supertool 'channel:health'                        # is anything receiving these?
./supertool 'channel:probe'                         # ...and does the path work right now?
./supertool 'radar'                                 # prune, heal, report
./supertool 'radar:--state'                         # look without healing
```

`watches` and `radar` mainly answer questions about the *producing* half, but
neither renders a fleet emitting into a dead consumer as a healthy one any more
(#1183): `watches` carries a `DELIVERY` column and `radar` a one-line header,
both read from each watcher's own `last_emit` and from nothing else. Four values
— `accepted`, `NO LISTENER`, `unknown`, `no emit` — with no threshold and no
clock involved, and neither surface ever stops, re-arms or reaps a poller on the
strength of them. Note that `NO LISTENER` is the *expected* state in a session
started without `--dangerously-load-development-channels server:claude-channel`,
which binds no reader at all.

`channel:health` is the question about the socket itself, answered in **five**
states — `FORWARDING`, `NOT DELIVERING`, `CANNOT DETERMINE`, `CONTRADICTED`
(#1192) and `BOUND, NOT SUBSCRIBED` (#1543) — because delivery into a Claude
session is not observable from outside it and a two-state answer would have to
guess. (This paragraph said "three" until #1593, two states after the fourth and
fifth shipped: a reader keying on the list as written would have treated a
finding as an unrecognised verdict.) See
[docs/presets/watch.md](../../docs/presets/watch.md#is-it-delivering--channelhealth-554).

`channel:probe` is the same subject and a different question: everything
`channel:health` reads was written by traffic that already happened, so with no
traffic of its own it cannot say whether the path works *now* — a consumer
wedged on its read loop publishes exactly the numbers of an idle one. `probe`
writes one synthetic event, under a reserved source and with no watcher state
file, and reports which of the consumer's counters moved. It never renders
`forwarded` as arrival; it names the tag you should now look for and stops,
because the last leg is visible only from inside the receiving session (#1593).
See [docs/presets/watch.md](../../docs/presets/watch.md#does-it-work-right-now--channelprobe-1593).

`watches` says which pollers are alive; `radar` says what is *true*. Pollers
die with the machine and events are fire-and-forget, so at session start an
event-driven view knows nothing — which renders identically to "all green".
`radar` treats live GitLab as authoritative and the state files as cache: it
prunes terminal state files, flags drift where `last_event` fired on an older
pipeline than `source_state` reports, respawns watchers for open MRs that lost
theirs, then prints a full board (cold start) or the delta. Idempotent — run it
on every session start. Details in [docs/presets/watch.md](../../docs/presets/watch.md).

Two tiers ship: `gl-mrs` (the GitLab MR board, above) and `gh-prs` (the GitHub PR
board, #859) — the latter adds the repository's **default branch** as a board
member, because a green PR is a statement about its merge base and nothing else
watches `master` after a squash lands. Neither is a default; radar refuses until
`ops.radar.radar_tiers` names one.

`radar` heals, and healing forks pollers. `radar:--state` is the same tier list
read-only — resolved scope, snapshot file, live pollers, feed scopes — reading
files already on disk and calling no API. It is an argument rather than a
separate op because `ops.radar.radar_tiers` merges into the op it is keyed by,
and a read-only view of a *different* tier list from the one radar runs is the
defect it exists to remove.

## Sources

| SOURCE            | ID is a…                       | Terminal when…                                 |
| ----------------- | ------------------------------ | ---------------------------------------------- |
| `gitlab-mr`       | GitLab MR iid                  | MR merged or closed                            |
| `github-pr`       | GitHub PR number               | PR merged or closed                            |
| `gl-pipeline`     | GitLab CI pipeline id          | pipeline success / failed / canceled / skipped |
| `gh-run`          | GitHub Actions run id          | run `status` reaches `completed`               |
| `gitlab-mr-feed`  | scope (`@me`, `@reviewer`, …)  | never — discovery has no end state             |
| `github-issue-feed` | scope (`@open`, `state=open,label=…`) | never — discovery has no end state    |
| `gl-runners`      | scope (`fleet`)                | never — a fleet has no end state               |

`gl-runners` is a **registered radar tier**, not a default one: radar only spawns it
when `ops.radar.radar_tiers` names it. See
[docs/presets/watch.md](../../docs/presets/watch.md) for the tier contract and for
which ops are worth watching at all.

The two **feed** sources are the exception: every other source polls **one
known id**, so none of them can discover something that did not exist when they
were spawned.

`gitlab-mr-feed` polls the whole population instead: new iids get a `gitlab-mr`
watcher and an `mr_opened` event, departed iids are looked up and reported as
what actually happened to them. `radar` keeps exactly one alive and says so when
it is not.

A population it could not establish — an unreachable GitLab, an expired token, a
scope carrying a filter token `gl-mrs` cannot apply — is **not** an empty one: it
emits one `mrs_unreachable` per outage and no departures, and keeps `known` so
recovery does not announce every open MR as new (#1602).

`github-issue-feed` polls a population of GitHub issues and has **no per-id
tier** — every fact worth watching on an issue (labels, assignees, comment
count, closure) is already in the list payload, so there is nothing for a
per-issue poller to add and no cross-tier duplicate to suppress. It is not
registered as a radar tier; spawn it yourself with the scope you care about.

## Transports

Pollers emit through three channels (all best-effort, none can crash the poller):

| Transport            | Purpose                                                              | Path                                                 |
| -------------------- | -------------------------------------------------------------------- | ---------------------------------------------------- |
| UDS socket (NDJSON)  | Live event stream — Phase 2 channel consumer reads this              | `/tmp/supertool-watch.sock`                          |
| Status file (JSON)   | Last-known state for the `watches` op + offline inspection           | `/tmp/supertool-watch-{source}__{id}.state.json`     |
| macOS osascript      | Desktop notification on terminal status (human-facing)               | system notification center                           |

**The socket path is overridable, and it must match on both ends.** Set
`SUPERTOOL_WATCH_SOCK` to redirect where a poller writes — it must be set to
the *same* path on the Phase 2 consumer for anything to arrive there (see
[notifiers/claude-channel/README.md](../../notifiers/claude-channel/README.md#start-up-and-socket-ownership)).
This is how a second Claude Code session gets a channel of its own after
`claude-channel` refuses to steal a live socket (#550), and it is how a
multi-user machine gives each session's *channel* a private path instead of the
world-traversable default. It does nothing about the pollers, which is the
next paragraph.

**`SUPERTOOL_WATCH_NAME` is the one-word way to do this, and the pair below is
what it derives (#1477).**

```bash
export SUPERTOOL_WATCH_NAME=oss
#   -> SUPERTOOL_WATCH_SOCK      = /tmp/supertool-watch-oss.sock
#   -> SUPERTOOL_WATCH_STATE_DIR = /tmp/supertool-watch-oss   (created 0700)
```

The state directory is created non-recursively, then held open
`O_RDONLY | O_DIRECTORY | O_NOFOLLOW`, `fchmod`ed to `0700` and checked for
ownership and mode through that descriptor — so `0700` is a fact about the
directory the pollers write into rather than about the moment it was made
(#1518; the same `presets/_image_root` boundary `gl-issue` uses). A symlink, a
file, a foreign-owned directory or one group or other can still reach is a
**refusal** naming which of those it was, and no poller slot is claimed there.

**Not closed:** `/tmp` is world-writable and the derived name is public, so
another local uid can take `/tmp/supertool-watch-<name>` before you do. You then
get the refusal rather than a write into their directory, and that channel stays
unusable until it is removed. That is the correct side of the trade and it is
named, not fixed — the same residual `docs/presets/gitlab.md` discloses for the
attachment root.

It is an ordinary environment variable, so a non-reserved key in an op's
`.supertool.json` block reaches it with no plumbing at all
(`docs/contributing.md`, "Extra config keys as environment variables"):

```json
{ "ops": { "radar": { "watch_name": "oss" },
           "watches": { "watch_name": "oss" },
           "channel": { "watch_name": "oss" },
           "watch": { "watch_name": "oss" },
           "unwatch": { "watch_name": "oss" } } }
```

**Declare it on all five, and `watch` and `unwatch` are the two people forget.**
A key in an op's block reaches only *that* op's subprocess, and those two are
the ops that spawn and kill pollers — declaring the name on `watches` alone
gives you a private board over a default-channel fleet, which is #1309's
half-configured state arriving through the config door instead of the
environment one. This repository shipped exactly that shape until #1732. Every
render now names the watch ops that declare nothing, so the gap is visible
rather than inferred.

**That configures five of the six surfaces, and the sixth is the one that
matters.** `claude-channel` is spawned by the harness from `.mcp.json`, never by
supertool, so no config key reaches it. Two routes do:

```sh
SUPERTOOL_WATCH_NAME=oss claude ...   # inherited by the servers the harness spawns
```

```json
{ "mcpServers": { "claude-channel": {
    "command": "bun",
    "args": ["${CLAUDE_PLUGIN_ROOT}/notifiers/claude-channel/channel.ts"],
    "env": { "SUPERTOOL_WATCH_NAME": "oss" } } } }
```

**Prefer the export, and never write the name into an `.mcp.json` that ships.**
A plugin's own `.mcp.json` installs for everybody, so a name true of one checkout
puts every other user's consumer on a socket their own pollers never bind — this
repository did exactly that and took it back out in
[#1541](https://github.com/Digital-Process-Tools/claude-supertool/issues/1541).
The `oss` plugin's `bin/oss-workspace` is the export route: it reads the one
`watch_name` the repository declares and exports it before launching. The `env` block is for an
`.mcp.json` you wrote for your own project root and ship to nobody.

A name in one file and not the other *is* the half-configured state, arriving
through a new door — so `channel:health` reads `.mcp.json` (plugin root and cwd)
and compares the socket it declares against the one this process uses. Four
states: they agree; they disagree, with both paths named; the file declares no
channel variable at all, so the consumer inherits an environment this op cannot
read and that is said as unknown; or the file could not be read. Only the first
is ever silent.

**Every board also says which project the name belongs to (#1732).** One name
means one socket and one poller-slot directory, so two projects under the same
name are one fleet — events from one project's pollers arrive on the other's
channel, and each board reports a population that is not its own. Until #1732
that rendered identically to a correct private fleet, and it was found in the
field: four repositories sharing one hand-copied
`"SUPERTOOL_WATCH_NAME": "oss-supertool"` in one machine's
`.claude/settings.local.json`.

On a **named** channel — the default paths are shared by construction and have
no owner to dispute — `watches`, `radar` and `channel:health` now read the
`.supertool.json` at or above the cwd and say what it claims, in **four states**
rather than two:

| the look found | what the board says |
| --- | --- |
| a declaration matching the name in force | `name oss is declared by <path> (ops: …) — this project's own channel` |
| a declaration of some *other* name | the name in force `is … not this project's channel`, both named |
| the project's own op blocks disagreeing | every declared name, and which one is in force |
| a config declaring no `watch_name` at all | the name `came from the environment` and the fleet may be another project's |
| no `.supertool.json` above the cwd | nothing here claims the name |
| a config that could not be parsed | **unknown, not unclaimed** — the read failed, so ownership was never established |

The last two are deliberately different answers. An absence produced by a failed
read is not an absence in the world, which is the rule `docs/validators.md`
§"Declining instead of guessing" states and the defect this preset has filed
most often.

**This changes nothing about what is in force.** The environment stays
authoritative, for the reason in the next paragraph; the attribution is a render,
and a render must never move a live fleet's paths.

**Precedence: an explicit `SUPERTOOL_WATCH_SOCK` or `SUPERTOOL_WATCH_STATE_DIR`
overrides the name, and the report says so.** Not because an export is more
authoritative in principle, but because it is the value a *running* poller
already captured and cannot migrate away from — making the name win would move
the paths underneath a live fleet. A name that loses to a stale export never
loses quietly. A name that is not usable as a path component is ignored, said
so, and the channel stays on the defaults rather than on half a private one.

**A second session needs `SUPERTOOL_WATCH_STATE_DIR` as well, and setting only
the socket is worse than setting neither (#1309).** The poller slot is a pid
file under the state directory, held `O_CREAT|O_EXCL` by exactly one process
(#476), so two sessions sharing the default `/tmp` share one set of slots: the
second session spawns no pollers at all, because the first session's already
hold every slot — and those are bound to the *other* socket for life. The board
renders healthy from both sides. `radar`'s delivery banner now compares each
watcher's recorded `sock_path` against the socket this process reads and says
so; see `docs/presets/watch.md`, "Two sessions on one machine".

A watcher spawned before the variable was changed keeps writing to whatever
`SOCK_PATH` it started with — that value is fixed for the process's
lifetime, and cannot migrate mid-run. It is recorded as `sock_path` in the
watcher's own state file (`/tmp/supertool-watch-{source}__{id}.state.json`),
alongside `only`, precisely so a watcher still bound to a stale path is
inspectable rather than a silent partial migration where some watchers
deliver and others don't while the board reads as healthy either way.

## Event payload (locked — Phase 2 depends on it)

```json
{
  "ts": "2026-05-24T19:00:00Z",
  "source": "gitlab-mr",
  "id": "21803",
  "event": "pipeline_failed",
  "payload": {
    "pipeline_id": "139928",
    "url": "https://gitlab.example.com/.../merge_requests/21803",
    "title": "feat: do the thing"
  }
}
```

### Size limits a producer must know about ([#605](https://github.com/Digital-Process-Tools/claude-supertool/issues/605))

The payload above is a contract about *shape*. It is also one about **size**,
because the Phase 2 consumer now enforces one — and a cap the producer does not
know about means pollers keep sending what the consumer keeps refusing.

| Limit | Default | Applies to |
| --- | --- | --- |
| 2,048 chars | per attribute | one value inside `payload` |
| 8,192 chars | per event | every attribute of one event, keys included |
| 1,048,576 chars | per line | one NDJSON line on the socket |
| 65,536 chars | per 60 s | everything delivered in a rolling minute, across every poller |
| 262,144 chars | per 60 s | the hard floor under that minute |

For scale: the largest payload observed across ten live watchers was **488
characters**, its longest value **117**. A poller that stays anywhere near real
data will never meet these. One that embeds a CI failure summary, a log excerpt,
or a field somebody widened later, will.

**Keep open-ended fields bounded at the source, with the bound visible in the
value.** `gitlab-mr` is the pattern: `observed_failed_jobs` ships at most five job
names and appends `+N more` *inside* the joined string, so a surface rendering
that one attribute cannot read five names as the whole story
(`FAILED_JOBS_MAX`, `sources/gitlab-mr/poller.py`).

The limits are enforced in one place — the consumer — rather than on both sides,
because two enforcement points that can disagree are worse than one. What the
consumer does when a poller exceeds them is documented in
[notifiers/claude-channel/README.md](../../notifiers/claude-channel/README.md#size-limits-and-how-a-clamped-event-says-so-605):
the oversized attribute is withheld whole (never truncated), the event is still
delivered, and it carries a `clamped` attribute naming what went and how big it
really was. Nothing is silently shortened, and nothing is silently lost.

**The last two rows are the one limit a poller can hit without any single event
being large.** They are a budget on the *stream*, shared by every watcher feeding
one session, so a chatty poller spends a quiet poller's minute too. Past 65,536
chars in a rolling minute events keep their routing and lose their payload,
carrying a `burst` attribute that says so; past 262,144 they are suppressed and
counted, and the count arrives on the next delivery. Neither is reachable by real
traffic — a ten-watcher fleet spawn is ~5 KB — but a poller that emits on every
tick instead of on every *change* will find them, and that is the bug the budget
makes visible rather than the budget being the bug. Emit on state change, which
is what `is_terminal`/`poll` are shaped for.

### Reserved payload keys ([#609](https://github.com/Digital-Process-Tools/claude-supertool/issues/609))

**A `payload` key may not be named after a field the bridge writes itself.** The
consumer sets the identifying fields — `watcher_source`, `id`, `event`, `ts`,
`first_tick` — and until #609 the payload was merged *over* them, so a poller
with a `payload.id` re-aimed the event at whatever that field held. Measured: an
event announced as `gitlab-mr 33173: pipeline_failed` was delivered as
`not-gitlab 11111: pipeline_succeeded`, with the body and the attributes in
perfect agreement and nothing on stderr.

The reserved names are:

| Key | Written by |
| --- | --- |
| `watcher_source`, `id`, `event`, `ts`, `first_tick` | the event's own routing fields |
| `source` | auto-injected by Claude Code from the MCP server name |
| `clamped`, `collided`, `unsendable` | the consumer's disclosures ([#605](https://github.com/Digital-Process-Tools/claude-supertool/issues/605), #609, [#612](https://github.com/Digital-Process-Tools/claude-supertool/issues/612)) |
| `__proto__` | cannot become an attribute at all — assigning it to a JS object is a silent no-op |

A payload key with one of those names is **ignored, and said so**: the event
carries a `collided` attribute and a body line naming the key, its size and the
reserved set, so a poller author finds out rather than wondering why their field
never arrives. The fix on the producer side is to rename the field —
`mr_id`, `event_kind`, `observed_ts` — not to hope it lands.

Note this also used to lose the *whole* event: a large `payload.id` overwrote the
routing id, which then failed the per-attribute cap, and the event was refused
with the stderr reason `routing key over 2048 chars` while the real routing id
was five digits. That cannot happen now.

### Payload values the bridge will not send ([#612](https://github.com/Digital-Process-Tools/claude-supertool/issues/612))

`_meta` is a string map, enforced by a schema on the receiving end — a
non-scalar reaching it throws inside Claude Code's notification handler
(#554). So `notifiers/claude-channel` only ever forwards a string, boolean, or
finite number. An object, array, `null`, `undefined`, `NaN`, or `Infinity`
payload value is refused: `String({})` is `"[object Object]"`, which reads
downstream as data a poller meant to send, and there is no honest way to
shorten or quote a structure instead.

That refusal is correct and stays. What changed is the silence: a refused key
used to vanish with nothing in the attributes, the body, or stderr. It now
carries an `unsendable` attribute naming the key and its *shape* (`object`,
`array`, `null`, ...) — never its contents, since quoting is exactly what was
refused. `gl-runners`'s `runner_added`/`runner_silent` events are the one live
instance measured before this was built: `payload.tags` is a `list[str]`.

If a payload legitimately needs to carry a list, join it into a string at the
source — the same pattern `observed_failed_jobs` already uses for
`gitlab-mr` — rather than relying on the bridge to render it.

### Payload strings are other people's words ([#819](https://github.com/Digital-Process-Tools/claude-supertool/issues/819))

`title`, `description`, `tags`, `branch`, `workflow`, `error` — whoever opened
the watched object chose those words, and on a public tracker that is anyone.
They land in `<channel>` attributes and in the body Claude reads as prose, in a
session whose MCP `instructions` tell it to investigate the event.

**A source does not have to remember this.** `transport.emit_event` flattens
every string it is handed, and every string inside any container it is handed —
lists, tuples and dicts, nested, to a bound of `FLATTEN_MAX_DEPTH` (6) levels —
so no field can grow a line that reads as the notifier's own. It is done at that
one call rather than in each `poller.py` on purpose: the sources are the part
that keeps being added to, and a rule that has to be re-applied by each new one
is a rule that gets missed. Do not pre-flatten in a poller and do not work
around it — a value that was already one line comes out byte-identical.

**Past the depth bound a value is refused, not passed through**
([#825](https://github.com/Digital-Process-Tools/claude-supertool/issues/825)).
It is replaced by `[supertool: value refused — nested deeper than 6 levels, so
it could not be flattened]`. Naming types was how the next poller's field got
missed once already: this walked `str` and `list` only, so a
`payload={"jobs": [{"error": ...}]}` reached the socket unflattened and the
consumer dropped the field rather than complaining. If your source needs a
shape deeper than six, flatten the structure rather than the strings.

## Lifecycle

Each `watch` invocation forks a detached poller process. The process IS the
subscription — no central config to manage:

- PID file per active watcher: `/tmp/supertool-watch-{source}__{id}.pid`
- `unwatch` reads the PID file, SIGTERM (then SIGKILL after 500ms), removes the file
- Stale PIDs swept by the `watches` op automatically
- Pollers auto-stop when the source declares the target terminal
  (`is_terminal(state) -> bool`)
- Pollers also stop after `MAX_CONSECUTIVE_POLL_FAILURES` (120) failed polls in
  a row, emit `watcher_gave_up`, release the pidfile and **keep** their state
  file so the board can say why ([#1852](https://github.com/Digital-Process-Tools/claude-supertool/issues/1852)).
  One successful poll starts the count again; a source overrides the bound with
  `MAX_CONSECUTIVE_FAILURES`. Full write-up in `docs/presets/watch.md`.

## Writing a new source

Drop a folder under `presets/watch/sources/<NAME>/`:

```
sources/your-source/
  events.json     # event vocabulary (introspection only)
  poller.py       # the polling implementation
```

### `events.json`

```json
{
  "source": "your-source",
  "events": [
    {"key": "happened",        "label": "Something happened"},
    {"key": "happened_again",  "label": "Something happened again"}
  ]
}
```

### `poller.py`

```python
INTERVAL = 30  # seconds between polls

def poll(state: dict, ctx: dict) -> tuple[list[dict], dict]:
    """Return (events_to_emit, new_state).

    ctx = {"source": "your-source", "id": "<watcher id>", "only": [...]}.

    Each event is a dict:
      {
        "event": "happened",
        "payload": {...},
        "notify_title": "Optional macOS notification title",
        "notify_message": "Optional macOS notification body",
      }

    Diff against `state`; return only NEW events. Returning the same event
    on every tick produces a notification storm.

    The framework persists `new_state` and passes it back on the next call.
    """
    ...

def is_terminal(state: dict) -> bool:
    """True if the watcher should stop on its own (e.g. MR merged)."""
    return False
```

The dispatcher handles PID files, signals, transport, the `only=` filter, and
state persistence. Source code stays focused on "what changed?".

## Phase 2 — channel consumer

Shipped at [`notifiers/claude-channel/`](../../notifiers/claude-channel/README.md).
It binds the UDS socket this preset writes to and pushes each event into a
running Claude Code session via the Channels feature. No changes to this
preset are required to wire it up — just install Phase 2 separately and
launch Claude with `--dangerously-load-development-channels server:claude-channel`.
