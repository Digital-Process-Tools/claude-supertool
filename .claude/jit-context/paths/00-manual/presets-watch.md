---
title: "presets/watch/ — pidfile sentinels, poller identity, radar --state, channel.health"
match: "presets/watch/"
mode: once, remind
---

# pidfile reads — `transport.py`

`read_pid_checked` / `read_state_checked`: `os.open(path, O_RDONLY | O_NOFOLLOW)`, fd closed on
the `fdopen` arm. Three return states:

| Return | Meaning |
| --- | --- |
| `(pid, "")` | file read, names a process |
| `(0, "")` | `ENOENT` — the honest absence |
| `(0, reason)` | file exists, content is not a PID — still reclaimable |
| `(None, reason)` | read failed for any other reason — **do not treat as free** |

`0` is the sentinel meaning *slot is free*. Returning it for "unreadable" spawns a duplicate
watcher (the 2026-08-01 flood: a real `pipeline_failed` unannounced 23min). `read_pid_checked` is
the **5th** call site of this read pattern (`transport.py:118`); the other 4 already carried the
same guard: `channel.read_health` (#1184/#1187), `channel._read_state_file`/`stranded_watchers`
(#1191), `read_state_checked` (#1197). Fixed in `read_pid_checked` by #1200.

`claim_pidfile` (`transport.py:200`) → `CLAIM_UNKNOWN` (`= -1`) on an unsettled claim (`None` from
the read, or any `OSError` other than "already exists"). `release_pidfile` unlinks only on
positive identification. `list_active_pids` (`transport.py:707`) does **not** unlink a pid file it
could not read — omits the row instead; `list_watchers`' process scan still surfaces the real
poller as `orphan`.

# poller identity — DECIDED, do not re-derive

`dispatcher._exec_labelled` (`dispatcher.py:612`) execs into a labelled argv (#511). This is
settled. **Re-deriving it cost a 212k-token agent run on #749.**

Processes spawned before labelling existed wear their parent's argv and are invisible to the scan
**by design**. Clear them by hand, per-PID (`kill <pid>`) — no clear-by-heuristic command exists.
Killing on inference has stopped two live watchers before.

# `radar:--state`

`main()` (`radar.py:389`) branches on `args[0] == "--state"` straight into `state_main` —
**spawns nothing, reaps nothing.** Safe inside a live worktree.

Plain `radar` is an action: `_spawner()` (`radar.py:239`) runs the reap
(`dispatcher.reap_duplicate_pollers()`) guarding the **first** spawn of the run (#957) — not off
`main()`, not on every tier. `radar_report` alone (no `_spawner` call) spawns and reaps nothing.

# `channel.health` (`channel.py:606`)

Three states: verified / `CONTRADICTED` (`RC_CONTRADICTED = 4`) / unable. Peer-pid check uses
`LOCAL_PEERPID` on macOS (`channel.py:129,252`) — **not** `LOCAL_PEERCRED`, which returns a
`struct xucred` with uid only, no pid (#1192).

`peer_credentials_supported()` (`channel.py:209`): False unless Linux+`SO_PEERCRED` or `darwin`.
FreeBSD has `LOCAL_PEERCRED` but not `LOCAL_PEERPID` → lands in the **unable** arm too — Windows is
not the only platform that can't answer.
