# `watch` preset

Background pollers for external sources (GitLab MRs, jobs, calendar, …) that
emit events when state changes. Reusable foundation — a Phase 2 channel
consumer will plug into the same UDS socket to push events directly into
Claude Code.

## Ops

```
watch:SOURCE:ID[:only=event1,event2]    spawn poller (fire-and-forget)
unwatch:SOURCE:ID                       kill poller, remove PID file
watches                                 list active pollers (table)
radar                                   reconcile coverage vs live GitLab, then report
```

Example:

```bash
./supertool 'watch:gitlab-mr:21803'                 # all events
./supertool 'watch:gitlab-mr:21803:only=pipeline_failed,merged'
./supertool 'watch:gl-pipeline:151111'              # poll a CI pipeline to completion
./supertool 'watches'
./supertool 'unwatch:gitlab-mr:21803'
./supertool 'radar'                                 # prune, heal, report
```

`watches` says which pollers are alive; `radar` says what is *true*. Pollers
die with the machine and events are fire-and-forget, so at session start an
event-driven view knows nothing — which renders identically to "all green".
`radar` treats live GitLab as authoritative and the state files as cache: it
prunes terminal state files, flags drift where `last_event` fired on an older
pipeline than `source_state` reports, respawns watchers for open MRs that lost
theirs, then prints a full board (cold start) or the delta. Idempotent — run it
on every session start. Details in [docs/presets/watch.md](../../docs/presets/watch.md).

## Sources

| SOURCE            | ID is a…                       | Terminal when…                                 |
| ----------------- | ------------------------------ | ---------------------------------------------- |
| `gitlab-mr`       | GitLab MR iid                  | MR merged or closed                            |
| `github-pr`       | GitHub PR number               | PR merged or closed                            |
| `gl-pipeline`     | GitLab CI pipeline id          | pipeline success / failed / canceled / skipped |
| `gitlab-mr-feed`  | scope (`@me`, `@reviewer`, …)  | never — discovery has no end state             |

Every source but the last polls **one known id**, so none of them can discover
an MR that did not exist when they were spawned. `gitlab-mr-feed` polls the
whole population instead: new iids get a `gitlab-mr` watcher and an
`mr_opened` event, departed iids are looked up and reported as what actually
happened to them. `radar` keeps exactly one alive and says so when it is not.

## Transports

Pollers emit through three channels (all best-effort, none can crash the poller):

| Transport            | Purpose                                                              | Path                                                 |
| -------------------- | -------------------------------------------------------------------- | ---------------------------------------------------- |
| UDS socket (NDJSON)  | Live event stream — Phase 2 channel consumer reads this              | `/tmp/supertool-watch.sock`                          |
| Status file (JSON)   | Last-known state for the `watches` op + offline inspection           | `/tmp/supertool-watch-{source}__{id}.state.json`     |
| macOS osascript      | Desktop notification on terminal status (human-facing)               | system notification center                           |

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

## Lifecycle

Each `watch` invocation forks a detached poller process. The process IS the
subscription — no central config to manage:

- PID file per active watcher: `/tmp/supertool-watch-{source}__{id}.pid`
- `unwatch` reads the PID file, SIGTERM (then SIGKILL after 200ms), removes the file
- Stale PIDs swept by the `watches` op automatically
- Pollers auto-stop when the source declares the target terminal
  (`is_terminal(state) -> bool`)

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
