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

# `gh-branch` streak sentence -- run-list arm still conflates attribution

`UNKNOWN_CONFIRM_STREAK`'s `else` arm (`poller.py:414-422`) still says "...empty run list, most
recently" -- misattributing every poll in a mixed streak to the run list. Only the `if` arm was
reworded cause-first (#2537, closed via #2548); this sentence defect is untracked.

# `github-pr-feed/poller.py:pr_only()` -- one `except Exception` covers two different failures (#2560)

`pr_only()`'s `except Exception: _pr_only_cache = []` (for a malformed `pr_exclude_events`) also
swallows an `ImportError`, missing `radar.py`, or a `read_tiers()` decode failure -- none
validated by radar -- silently dropping the event filter on every per-PR poller this feed forks.
`terminal_coverage(spawned=True)` then reports the `[]` as *known* rather than "could not tell".

# `gitlab-mr/poller.py:_fetch_approvals()` -- a confirm-tick fetch failure has no surfaced state (#2645)

`_fetch_approvals(iid)` returns `(None, error)` on any failure -- `_glab_api` erroring, a
non-dict payload, or a missing `approved` key. `poll()` binds that to `_approved_error` and never
reads it again: no line, no event, `approved` carries its last known value forward unchanged.
"Still not approved" and "the approvals endpoint has been failing on the one confirming tick,
repeatedly, for a week" render identically on the channel. Compare the *primary* MR fetch in the
same file: `_fetch` failing emits an explicit `mr_unreachable` event with `notify_title: f"!{iid}
-- cannot tell"` (`poller.py:410-429`) -- the new confirm-tick fetch has no equivalent. Adding a
fetch call to a poller in this directory without checking it has a mirrored surfaced-failure path
repeats this gap; `mr_unreachable` is the pattern to mirror, not a one-line `except: pass`. No longer capped at one tick (#2670): fires once per exit from `not_approved`, and again on every later poll while stored `approved` is a confirmed `False`.
