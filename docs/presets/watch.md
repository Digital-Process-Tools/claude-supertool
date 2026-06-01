# `watch` preset

Background pollers for external sources that emit events on state-change. The framework writes events to a UDS socket; consumers (macOS Notification Center, or the [claude-channel](../../notifiers/claude-channel/README.md) MCP server) pick them up and push them where you need them.

## Ops

```
watch:SOURCE:ID[:only=event1,event2]   spawn poller (fire-and-forget)
unwatch:SOURCE:ID                      kill the poller, remove PID file
watches                                list active pollers
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
# default: my failing GitLab MRs → gitlab-mr watchers
bash presets/watch/watch-mine.sh

# re-sync every 5 min from inside Claude Code
/loop 5m bash presets/watch/watch-mine.sh

# any feed op + source — e.g. my failing GitHub PRs
bash presets/watch/watch-mine.sh 'gh-prs:author=@me,failed,iids' github-pr
```

Args: `$1` feed op (default `gl-mrs:author=@me,failed,iids`), `$2` watch source (default `gitlab-mr`), `$3` notify events (default `pipeline_failed,merged`). The separation is deliberate — the list op owns *what's mine* (a platform concern), the watch preset stays generic. The feed op just has to emit bare ids (the `iids` flow); both `gl-mrs` (GitLab) and `gh-prs` (GitHub) ship today.

## Bundled sources

| Source | Polls | Events |
|---|---|---|
| `github-pr` | `gh pr view <N> --json state,mergeable,reviewDecision,statusCheckRollup,comments,...` | `checks_failed`, `checks_succeeded`, `checks_pending`, `review_approved`, `review_changes_requested`, `comment_added`, `merged`, `closed`, `conflicts_appeared` |
| `gitlab-mr` | `glab api projects/:id/merge_requests/<iid>` | `pipeline_failed`, `pipeline_succeeded`, `pipeline_running`, `merged`, `closed`, `conflicts_appeared` |

Each source declares its event vocabulary in `presets/watch/sources/<NAME>/events.json` for introspection.

## Transports

Pollers emit through three channels (all best-effort, none can crash the poller):

| Transport | Path | Purpose |
|---|---|---|
| UDS socket (NDJSON) | `/tmp/supertool-watch.sock` | Live event stream — consumers read this |
| Status file (JSON) | `/tmp/supertool-watch-{source}__{id}.state.json` | Last-known state for `watches` op + offline inspection |
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

## Lifecycle

Each `watch` invocation forks a detached poller process. The process IS the subscription — no central config:

- PID file per active watcher: `/tmp/supertool-watch-{source}__{id}.pid`
- `unwatch` reads the PID file, SIGTERM (then SIGKILL after 200ms), removes the file
- Stale PIDs swept by the `watches` op automatically
- Pollers auto-stop when the source declares the target terminal (`is_terminal(state) -> bool`)

`SOURCE` and `ID` must not contain `__` (reserved as the filename separator).

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
