# `watch` preset

Background pollers for external sources that emit events on state-change. The framework writes events to a UDS socket; consumers (macOS Notification Center, or the [claude-channel](../../notifiers/claude-channel/README.md) MCP server) pick them up and push them where you need them.

## Ops

```
watch:SOURCE:ID[:only=event1,event2]   spawn poller (fire-and-forget)
unwatch:SOURCE:ID                      kill the poller, remove PID file
watches                                list active pollers
radar                                  reconcile coverage vs live GitLab, then report
```

Example:

```bash
./supertool 'watch:github-pr:179'                        # all events
./supertool 'watch:github-pr:179:only=checks_failed'     # filter
./supertool 'watch:gitlab-mr:21803:only=pipeline_failed,merged'
./supertool 'watches'
./supertool 'unwatch:github-pr:179'
```

Composable in one batched call:

```bash
./supertool 'watch:github-pr:179' 'watch:github-pr:180' 'watches'
```

## Watch many from a query — `watch-mine.sh`

`watch:SOURCE:ID` watches one id. To watch *every id a query returns* — e.g. all your failing MRs — pair it with a list op via the bundled supervisor `presets/watch/watch-mine.sh`. It runs a "list mine" op (`gl-mrs`/`gh-prs`), extracts the ids, and spawns one watcher each. Idempotent (the `watch` op skips ids already watched), so it's safe on a loop:

```bash
# default: every open GitLab MR of mine → gitlab-mr watchers
bash presets/watch/watch-mine.sh

# re-sync every 5 min from inside Claude Code
/loop 5m bash presets/watch/watch-mine.sh

# any feed op + source — e.g. my failing GitHub PRs
bash presets/watch/watch-mine.sh 'gh-prs:author=@me,failed,iids' github-pr
```

Args: `$1` feed op, `$2` watch source, `$3` notify events. The defaults live in `presets/watch/defaults.py` — one place, read by both this script and the `radar` op so a healed watcher is identical to a spawned one:

| Default | Value | Why |
|---|---|---|
| feed | `gl-mrs:author=@me,state=opened,iids` | Every open MR, not just the failing ones. A watcher can only report an MR *going* red if it was already watching while the MR was green. |
| source | `gitlab-mr` | |
| only | `pipeline_failed,pipeline_succeeded,merged,closed,conflicts_appeared` | `pipeline_succeeded` closes the red → fix → push → *?* loop, and is the only proof an automated fix worked. `pipeline_running` is excluded (you just pushed; no information) and `comment_added` is excluded because `user_notes_count` counts system notes. |

The separation is deliberate — the list op owns *what's mine* (a platform concern), the watch preset stays generic. The feed op just has to emit bare ids (the `iids` flow); both `gl-mrs` (GitLab) and `gh-prs` (GitHub) ship today.

## `radar` — reconcile, don't just report

`watches` reports that pollers are alive. It cannot report what is *true*, and the two diverge routinely:

```
last_event    : pipeline_failed  on pipeline 154177
source_state  : running          on pipeline 154180
```

**`source_state` is truth; `last_event` is history.** A board built on `last_event` calls that MR broken when it is mid-retry on a newer pipeline.

Events also cannot survive a session boundary: the transport is fire-and-forget so an event emitted with no listener is gone permanently, pollers are processes that die with the machine, and pollers stop themselves on terminal state by design. At the start of a session an event-driven view knows *nothing* — and "knows nothing" renders identically to "everything is green". So state is the floor and events are the optimisation, not the reverse.

`radar` is therefore an idempotent reconcile, safe on every session start:

```
1. live truth   one gl-mrs query for open MRs — authoritative. State files are
                cache and may be absent or hours stale.
2. reconcile    prune state files whose watcher reached a terminal state; flag
                drift where the last event fired on an older pipeline.
3. heal         respawn a watcher for every open MR with no live poller —
                covers reboot, crash, a cleared /tmp, and MRs that were green
                when watchers were last spawned.
4. report       full board on cold start, delta-only afterwards.
```

```bash
./supertool 'radar'
```

```
radar: cold start — no prior snapshot, full board
👁 ✗ test_unit_dpt +5   ·   4h   12Δ  !33161  SiNotificationConfiguration scaffold
👁 ● running            ✓   5m    3Δ  !33173  Generator loadable + coverage   [drift: 154177→154180]
  ✓ ok                  ✓  39m    1Δ  !33172  docs(vocab): CKEditor          [healed]

3 open | 1 failing | 1 running | 1 green | 3 watched | 1 healed | 1 drift | 2 pruned
```

Rows use the same format as `gl-mrs`, plus two marks radar alone can report:

| Mark | Meaning |
|---|---|
| `[drift: A→B]` | the last event fired on pipeline A, but pipeline B is current — the event is stale history |
| `[healed]` | this open MR had no live poller; radar respawned one |
| `[unwatched]` | radar could not respawn a poller — a real coverage gap |

**"Nothing moved"** means the set of open MRs is unchanged, no MR changed pipeline status / pipeline id / draft / conflict flag, and radar took no action. Then radar prints one summary line — not nothing:

```
radar: no change | 7 open | 7 watched
```

Total silence would be indistinguishable from a radar that failed to run, which is the failure this op exists to remove. For the same reason an unreachable GitLab is a hard error (exit 1, no board, nothing pruned or healed) rather than an empty green board. Standing failures and conflicts are re-printed even when unchanged — an unfixed red is a current fact, not history.

## Bundled sources

| Source | Polls | Events |
|---|---|---|
| `github-pr` | `gh pr view <N> --json state,mergeable,reviewDecision,statusCheckRollup,comments,...` | `checks_failed`, `checks_succeeded`, `checks_pending`, `review_approved`, `review_changes_requested`, `comment_added`, `merged`, `closed`, `conflicts_appeared` |
| `gitlab-mr` | `glab api projects/:id/merge_requests/<iid>` | `pipeline_failed`, `pipeline_succeeded`, `pipeline_running`, `merged`, `closed`, `conflicts_appeared` |
| `gitlab-mr-feed` | `glab mr list` for a whole filter | `mr_opened`, `mr_merged`, `mr_closed`, `mr_left_feed` |

Each source declares its event vocabulary in `presets/watch/sources/<NAME>/events.json` for introspection.

## Discovery — `gitlab-mr-feed`

Every source above polls **one known id**, so nothing in a running watch session can discover an MR that did not exist when the poller was spawned. Observed: !33176 opened between two `radar` runs, and only the second run found it. In between, the board was not visibly wrong — it was confidently complete and missing an MR, which renders identically to all-green.

`gitlab-mr-feed` polls the *population* instead of a member of it:

```bash
./supertool 'watch:gitlab-mr-feed:@me'          # everything I author
./supertool 'watch:gitlab-mr-feed:@reviewer'    # everything I owe a review
./supertool 'watch:gitlab-mr-feed:milestone=v18.9,state=opened'
```

The ID is a **scope**: `@me` and `@reviewer` are aliases for `gl-mrs` filters, anything else is used as a literal filter. Per poll it makes one `glab mr list` call with no pipeline enrichment — the feed answers *which MRs exist*, and the per-MR watcher it spawns answers *what is happening to this one*.

| Behaviour | |
|---|---|
| interval | 300s. MRs appear on human timescales; the per-MR pollers already carry the 30s traffic. |
| first poll | Records the baseline **silently**. Announcing every MR that was already open is not discovery, it is a notification storm. Watchers are still spawned. |
| new iid | `watch:gitlab-mr:<iid>` with the shared `DEFAULT_ONLY` filter, plus an `mr_opened` event and a desktop ping — nothing else can report it. |
| known iid with a dead watcher | Respawned. Coverage is continuous for the same reason discovery is. |
| vanished iid | One `glab api` lookup, then `mr_merged` / `mr_closed` / `mr_left_feed` — the terminal two only when no per-MR poller announces that transition itself. |
| `is_terminal` | Never. A population has no final state. |
| fetch failure | No events, state kept. An unreachable GitLab must never read as "everything vanished". |

**Vanished is not merged.** An iid leaving `author=@me,state=opened` could have merged, closed, been reassigned, or the filter could have changed. Guessing `mr_merged` is right most of the time and confidently wrong the rest, so the feed spends one lookup on the truth — departures are rare, so the call is too. Only a confirmed `merged`/`closed` stops the watcher and, since the per-MR watcher reaches the same conclusion within 30s and owns the desktop ping, `mr_merged`/`mr_closed` are emitted on the wire **without** a notification so a merge never pings twice. A still-open MR gets `mr_left_feed` and **keeps its watcher** — following an MR the feed no longer returns is legitimate, the same distinction `radar`'s prune already makes.

### One transition, one line

The feed and the per-MR pollers are two independent layers over the same fact, so a single merge used to arrive twice — `merged` from the poller, `mr_merged` from the feed, seconds apart under different event keys. The board renders one line per event, and duplicates train the reader to skim, which is the failure mode that makes a real red get missed.

**The feed emits `mr_merged` / `mr_closed` only when no per-MR poller announces that transition itself.** Everything else about the feed is unchanged: `mr_opened` and `mr_left_feed` have no per-MR twin and are never suppressed, and suppression is keyed strictly per iid.

*Liveness at the moment of the departure cannot decide this*, in either direction, which is why the answer is recorded earlier:

- Reporting `merged` is exactly what makes a per-MR poller terminal and ends it, so by the time an iid leaves the population its reporter is usually **already gone** — along with its state file. "Reported and exited" and "never existed" look identical from there.
- A poller that *is* still alive is precisely the one that has **not** spoken yet. And its `only=` filter may exclude `merged` entirely, in which case it is alive, healthy, and will never say a word about the merge.

So each poll records, per iid still in the population, which terminal events a per-MR poller for it will announce — taken from the `only` filter that poller now **publishes into its state file** (`{"only": ["pipeline_failed", "merged", …]}`). An empty list means "emits everything"; the key being **absent** means nobody could tell us, which is a different answer and must not resolve the same way.

**Every unanswerable case resolves to *not covered*, so the feed reports.** A duplicate is visible and cheap; a terminal transition nobody reports is a radar that stopped reporting without anyone noticing. This is the same bias `presets/_proc.py` encodes when an unanswerable liveness question resolves to *not alive* — it lands here as "fall back to a second line", never "fall back to silence". Concretely, the feed remains the sole and unsuppressed reporter when it runs without per-MR pollers behind it (a spawn that failed, a poller filtered away from `merged`, an unreadable state file, or a `known` entry written by a feed that predates this behaviour).

A live poller that owes us the event is also **no longer stopped** on the terminal path. `stop_watcher` SIGTERMs then SIGKILLs, and killing the canonical reporter before it has reported is how one suppressed duplicate becomes no report at all; the unwatch stays on the branch where the feed does the reporting, which is the stale-PID cleanup it was always meant to be.

`radar` ensures exactly one feed poller is alive on every run (a live PID short-circuits the spawn — N radar runs, one feed), and reports it in the footer as `feed ok` / `feed respawned` / `feed DOWN`. A feed that is down, or alive but erroring every tick, gets an explicit `WARNING` line that survives delta suppression — a feed that stopped discovering looks exactly like a day on which nothing happened.

## Transports

Pollers emit through three channels (all best-effort, none can crash the poller):

| Transport | Path | Purpose |
|---|---|---|
| UDS socket (NDJSON) | `/tmp/supertool-watch.sock` | Live event stream — consumers read this |
| Status file (JSON) | `/tmp/supertool-watch-{source}__{id}.state.json` | Last-known state for `watches` op + offline inspection, plus the poller's own `only` filter so another tier can tell what it will ever emit |
| macOS osascript | system notification center | Desktop ping on terminal status / error |

Override the socket path with `SUPERTOOL_WATCH_SOCK` env var (must be set on both pollers and the consumer).

## Event payload (locked contract)

```json
{
  "ts": "2026-05-24T19:00:00Z",
  "source": "github-pr",
  "id": "179",
  "event": "checks_failed",
  "payload": {
    "url": "https://github.com/.../pull/179",
    "title": "feat: do the thing"
  }
}
```

Consumers can rely on `ts/source/id/event/payload` always being present. Extra fields inside `payload` vary by source — see each source's `events.json` and `poller.py`.

**The wire record itself has not moved.** What changed with [#434](https://github.com/Digital-Process-Tools/claude-supertool/issues/434) is *which* records get emitted, not their shape: no field was added, removed or retyped, and every event key in every `events.json` still means what it did. The contract that moved is the one **between the two GitLab tiers** — `gitlab-mr-feed` no longer emits `mr_merged`/`mr_closed` for an iid whose per-MR poller announces `merged`/`closed` itself, so a consumer that counted on receiving both keys for one merge now receives exactly one. Consumers wanting every terminal transition should keep treating `merged`/`mr_merged` (and `closed`/`mr_closed`) as the same fact under two keys — which is what they always were.

The status file gained one field, `only`: the event filter its poller was spawned with, as a list. `[]` means unfiltered (every event). Absent means the file was written by a poller that predates the field, and is not evidence that the poller emits everything.

## Lifecycle

Each `watch` invocation forks a detached poller process. The process IS the subscription — no central config:

- PID file per active watcher: `/tmp/supertool-watch-{source}__{id}.pid`
- `unwatch` reads the PID file, SIGTERM (then SIGKILL after 200ms), removes the file
- Stale PIDs swept by the `watches` op automatically
- Pollers auto-stop when the source declares the target terminal (`is_terminal(state) -> bool`)

`SOURCE` and `ID` must not contain `__` (reserved as the filename separator) or `/` (both are interpolated into a `/tmp` path).

## Writing a new source

Drop a folder under `presets/watch/sources/<NAME>/`:

```
sources/your-source/
  events.json   # event vocabulary (introspection)
  poller.py     # the polling implementation
```

### `events.json`

```json
{
  "source": "your-source",
  "events": [
    {"key": "happened",       "label": "Something happened"},
    {"key": "happened_again", "label": "Something happened again"}
  ]
}
```

### `poller.py`

```python
INTERVAL = 30  # seconds between polls

def poll(state: dict, ctx: dict) -> tuple[list[dict], dict]:
    """Return (events_to_emit, new_state).

    ctx = {"source": "...", "id": "...", "only": [...]}.

    Diff against `state`; return only NEW events. Returning the same event
    every tick produces a notification storm. The framework persists
    `new_state` and passes it back next call.

    Each event is a dict:
      {
        "event": "happened",
        "payload": {...},
        "notify_title": "Optional macOS notification title",
        "notify_message": "Optional macOS notification body",
      }
    """
    ...

def is_terminal(state: dict) -> bool:
    """True if the watcher should stop on its own (e.g. PR merged)."""
    return False
```

### Reuse an existing op's CLI helper

Most sources wrap a CLI tool (`gh`, `glab`, …) that an existing supertool op already wraps cleanly. Don't duplicate the wrapping — import the helper directly:

```python
import importlib.util
from pathlib import Path

_PR_MODULE_PATH = Path(__file__).parents[3] / "github" / "pr.py"
_spec = importlib.util.spec_from_file_location("github_pr_op", _PR_MODULE_PATH)
_pr_op = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pr_op)
_gh = _pr_op._gh  # single source of truth for gh invocation
```

`gitlab-mr` does the same with `_glab_api` from `presets/gitlab/mr.py`.

The framework handles PID files, signals, transport, the `only=` filter, and state persistence. Source code stays focused on *what changed*.

## Async wake into Claude Code

The `watch` preset alone gives you desktop notifications + queryable status. To make Claude *react* to events in real time, install the companion [claude-channel](../../notifiers/claude-channel/README.md) MCP server. It binds the UDS socket and pushes each event into a running Claude Code session via the [Channels feature](https://code.claude.com/docs/en/channels.md).

End-to-end (see [claude-channel/README.md](../../notifiers/claude-channel/README.md) for the install steps):

```
poller → /tmp/supertool-watch.sock → claude-channel MCP server → <channel> tag in Claude
```
