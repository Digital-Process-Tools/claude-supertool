---
title: "presets/watch/ — pidfile sentinels, poller identity, radar --state, channel.health"
match: "presets/watch/"
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

# the scan has three buckets — `poller_census` (#1881)

`scan_poller_pids` returns **only this channel's** pollers and that contract is fixed: every
caller of it acts (unwatch's multi-kill, the reap's signal), and widening it is a cross-channel
kill (#1514). `transport.poller_census()` is the whole scan — `mine` / `other` (keyed by channel
token) / `unknown` (no `chan=`) / `scan_ok`. `watches` renders the last two as **counts, never
rows**; naming a SOURCE/ID there is what invites `unwatch` against somebody else's slot.

**A test that renders the board must stub `poller_census`, not `scan_poller_pids`.** Stubbing the
narrow one leaves the other two buckets reading the real process table, so the test is green on a
quiet machine and red whenever anything is polling — including this repo's own PR watchers. Both
`_quiet_fleet` helpers had exactly that hole. `transport.empty_census(scan_ok)` is the seam.

`empty_census(False)` and `empty_census(True)` are different answers: nobody looked vs. looked and
found nothing. When `scan_ok` is False the board prints no count at all, because `0 pollers
elsewhere` off a scan that never ran is the defect this preset keeps filing.

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

# `gh-branch` streak sentence -- "most recently" still conflates the arm it names (#2537)

`poller.py`'s `UNKNOWN_CONFIRM_STREAK` branch fires when `raw_needs_guard = raw_is_no_run or
raw_is_unread_jobs` has held for N consecutive polls -- two independent failure boundaries (an
empty `gh run list`, a missing `gh run view --json jobs`) share one counter, so a streak crossing
the threshold need not be homogeneous. The `raw_is_unread_jobs` arm was reworded to name its own
cause ("...failed to establish a leg count, most recently because the job list did not come
back"). The `else` arm (open, unfixed) still reads "...have now come back with an empty run
list, most recently" -- grammatically that attributes an empty run list to every one of the N
polls, not just the most recent one, the same conflation the `if` arm was fixed to avoid. A
streak of [job-list miss, empty run list] hits this arm and tells the operator both polls
returned empty when one did not, sending them to `gh run list` when the failing call was `gh run
view --json jobs`.

# `github-pr-feed/poller.py:pr_only()` -- one `except Exception` covers two different failures (#2560)

`pr_only()` wraps its whole tier-resolution in `except Exception: _pr_only_cache = []`, cached
for the process lifetime. Justified in the docstring for a malformed `pr_exclude_events`
("radar already validated it once") -- but the same bare `except` also swallows an `ImportError`
loading the tier module, a missing `radar.py`, or a `read_tiers()` decode failure, none of which
radar validated. On any of those, every per-PR poller this feed forks gets no event filter at
all, silently and permanently, and `terminal_coverage(spawned=True)` then reports `pr_only()`'s
`[]` as a *known* filter (operator configured no exclusions) rather than "could not tell" --
same `[]` renders for both causes, no diagnostic either way. Narrow the `except` to what radar
actually validates, or give the except path a distinct could-not-resolve state
`terminal_coverage` can read as unknown.
