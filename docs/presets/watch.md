# `watch` preset

Background pollers for external sources that emit events on state-change. The framework writes events to a UDS socket; consumers (macOS Notification Center, or the [claude-channel](../../notifiers/claude-channel/README.md) MCP server) pick them up and push them where you need them.

## Ops

```
watch:SOURCE:ID[:only=event1,event2]   spawn poller (fire-and-forget)
unwatch:SOURCE:ID                      kill the poller, remove PID file
watches                                list active pollers, and any slot that lost one
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
| only | `pipeline_failed,pipeline_succeeded,comment_added,merged,closed,conflicts_appeared` | `pipeline_succeeded` closes the red → fix → push → *?* loop, and is the only proof an automated fix worked. `pipeline_running` is excluded — you just pushed, so it carries no information. `comment_added` joined the set in [#519](https://github.com/Digital-Process-Tools/claude-supertool/issues/519); it was held out on a belief about `user_notes_count` that turned out to be false ([below](#comment_added-is-in-the-default-set-519)). |

The separation is deliberate — the list op owns *what's mine* (a platform concern), the watch preset stays generic. The feed op just has to emit bare ids (the `iids` flow); both `gl-mrs` (GitLab) and `gh-prs` (GitHub) ship today.

## `radar` — reconcile, don't just report

> **Since [#528](https://github.com/Digital-Process-Tools/claude-supertool/issues/528) the MR board is a registered tier, not a default.** A bare `radar` with no `ops.radar.radar_tiers` refuses and tells you what to add — see [Radar tiers](#radar-tiers--everything-is-a-tier-nothing-is-a-default-528). Everything in this section describes the `gl-mrs` tier.

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

scope author=@me,state=opened (default) | 3 open | 1 failing | 1 running | 1 green | 3 watched | 1 healed | 1 drift | 2 pruned
```

Rows use the same format as `gl-mrs`, plus two marks radar alone can report:

| Mark | Meaning |
|---|---|
| `[drift: A→B]` | the last event fired on pipeline A, but pipeline B is current — the event is stale history |
| `[healed]` | this open MR had no live poller; radar respawned one |
| `[unwatched]` | radar could not respawn a poller — a real coverage gap |

### Effective scope is stated, never implied ([#486](https://github.com/Digital-Process-Tools/claude-supertool/issues/486))

The filter is an argument in the `gl-mrs` vocabulary, and it lives for **one invocation only**:

```bash
./supertool 'radar'                                              # defaults.DEFAULT_FILTER
./supertool 'radar:author=modular.system'                        # what the agent opened
./supertool 'radar:author=@me,author=modular.system,state=opened' # two queries, unioned by iid
```

Nothing is persisted. A session that deliberately widened the board and then runs a bare `radar` gets the **default population back**, and the narrowed board renders exactly like a board with nothing to report — the omission is produced by the tool and reads as an absence in the world. So every board names the scope it was built from, the default one included:

```
scope author=@me,state=opened (default) | 7 open | 7 watched | feed ok
scope author=modular.system | 2 open | 1 failing | 2 watched | feed ok
```

The `(default)` token is the distinction that was missing: an unlabelled board used to spell both "this is the default population" and "nobody said which population this is".

**A live feed poller on another scope is named too.** Changing the filter respawns the feed watcher and does not retire the old one, so effective scope ends up split between what the last invocation passed and what a still-running watcher was started with:

```
radar: NOTE — a feed poller is also live on scope 'author=@me,author=modular.system,state=opened',
which this board does not cover. Its MRs are not on this board; re-run as
radar:author=@me,author=modular.system,state=opened to see them.
```

Named, **not killed**. Two populations at once is a legitimate arrangement — the same reason `prune_terminal` refuses to prune a per-MR watcher merely for being outside this filter — so radar reports the split rather than resolving it. `./supertool 'watches'` lists the same pid files directly and spawns nothing.

**What this does not do.** It gives visibility, not continuity: a bare `radar` after a wide one still covers the default, and re-widening still means re-typing the filter. That is deliberate. Persisting the filter would mean a board whose population comes from a file nobody in the session chose, which is the same hidden state that produced the split scope in the first place.

### `[conflict]` vs `[empty]` ([#471](https://github.com/Digital-Process-Tools/claude-supertool/issues/471))

`has_conflicts` is **not** a conflict field — it is an alias for `cannot_be_merged?`, which GitLab annotates as also covering `has_no_commits?` and `branch_missing?`. Rendering it as `[conflict]` claimed a merge conflict on MRs that have no diff at all, which is [#465](https://github.com/Digital-Process-Tools/claude-supertool/issues/465)'s false positive on a second surface.

A blocked MR is now labelled by *what* blocks it:

| Flag | Meaning |
|---|---|
| `[conflict]` | blocked, and there is a diff that can conflict — unchanged meaning, it just fires less often now |
| `[empty]` | blocked with **positive evidence of an empty diff**: `detailed_merge_status: commits_status`, a null `sha`, or `diff_refs.head_sha` null or equal to `base_sha`. Nothing to conflict with — the source branch carries no commits the target lacks |

**`[empty]` is a new flag value; `[conflict]` is not renamed.** Anything grepping `[conflict]` keeps working and now gets fewer false hits. Renaming the column to `[blocked]` was considered and rejected: it is honest about the field but merges "you have a real conflict to resolve" and "you forgot to push" into one word, discarding the severity difference a triage board exists to show — and it breaks every existing grep to do it.

**The empty MR is still reported, not suppressed.** It is genuinely unmergeable, so it stays in the standing-problem set (re-printed even when unchanged, and named `empty` / `failed+empty` on the exclusion line). Suppressing it would trade a mislabel for a silent omission, which is the worse of the two here ([#445](https://github.com/Digital-Process-Tools/claude-supertool/issues/445)/[#454](https://github.com/Digital-Process-Tools/claude-supertool/issues/454)/[#414](https://github.com/Digital-Process-Tools/claude-supertool/issues/414)).

**`detailed_merge_status == "conflict"` is not used as a gate**, only as a widener. It reports the *first* failing mergeability check, and in `MergeRequest.all_mergeability_checks` the draft check runs second while conflict runs dead last — so a genuinely conflicted draft reports `draft_status`. Gating on it would silently stop reporting real conflicts on drafts and on MRs with unresolved threads.

**The snapshot key changed shape.** `conflict` in the radar snapshot was a bool and is now `"conflict"` / `"empty"` / `""`. The first run after upgrading reads every row with a stored `false` as moved and prints one full board. That is a one-time cost, not a defect.

**"Nothing moved"** means the set of open MRs is unchanged, no MR changed pipeline status / pipeline id / draft / conflict flag, and radar took no action. Then radar prints one summary line — not nothing:

```
radar: no change | scope author=@me,state=opened (default) | 7 open | 7 watched
```

Total silence would be indistinguishable from a radar that failed to run, which is the failure this op exists to remove. For the same reason an unreachable GitLab is a hard error (exit 1, no board, nothing pruned or healed) rather than an empty green board. Standing failures and conflicts are re-printed even when unchanged — an unfixed red is a current fact, not history.

### Standing exclusions

That last rule has a cost. An MR that is red for a reason nobody intends to fix soon — a long-lived branch, an infra-only pipeline failure, an experiment — reprints on every render and every session start. Suppressing it by *remembering* to ignore it is the worst available option: a permanently-red row that must be mentally filtered is what trains a reader to skim the board, and skimming is how a real red gets missed.

`ops.radar.radar_exclusions` in `.supertool.json` moves that rule into the tool. The key merges into the `radar` op the same way `ops.gl-job.job_patterns` does, and reaches the preset as `SUPERTOOL_RADAR_EXCLUSIONS`:

```json
{
  "ops": {
    "radar": {
      "radar_exclusions": {
        "19509": {
          "reason": "MySQL service TLS failure + standing conflict, not this MR",
          "until": "2026-09-01"
        },
        "20144": "spike branch, red on purpose"
      }
    }
  }
}
```

Keys are MR iids, matched exactly — excluding `1950` does not touch `19509`. The value is either a bare reason string or an object with `reason` and an optional ISO `until`. **`reason` is mandatory**: an exclusion nobody had to justify is exactly the one that becomes a permanent blind spot, so an unreasoned entry is refused and its row renders.

```
  ✓ ok    ✓  39m   1Δ  !33172  docs(vocab): CKEditor

1 open | 1 green | 1 watched | 1 excluded | feed ok
radar: excluded !19509 failed+conflict, still watched — MySQL service TLS failure, not this MR
```

**An exclusion hides a row; it must never hide the fact.** This is the one feature in `radar` that can conceal a failure, so it is built to be the opposite of a silent omission:

| Guarantee | How |
|---|---|
| **Accounted for** | the footer carries an `N excluded` token and one line names each suppressed MR, its *current* status (`failed`, `conflict`, `failed+conflict`) and its configured reason — on every run, including a delta-suppressed one |
| **Tallies match the board** | `open` / `failing` / `green` / `watched` count the rows that were printed, so `1 failing` is never a count with no row behind it; `N excluded` restores the total |
| **Self-expiring** | an exclusion only ever suppresses a *standing problem*. The moment the MR is green and unconflicted the suppression lifts by itself and the board prints `NOT applied — the reason is spent`. Same for an expired `until`, and for an iid that is no longer in the population |
| **Fails open** | unparseable JSON, a non-iid key, a missing reason, an unknown value shape — every unanswerable case resolves to *show the row*, and says why |

**An exclusion does not narrow the population.** This deliberately breaks the one-filter symmetry [above](#radar--reconcile-dont-just-report): the filter says what radar is *responsible for*, and radar does not stop being responsible for an MR because its row is noisy. So an excluded MR is still healed into the watcher fleet, still covered by the feed, still recorded in the snapshot, and its watcher still emits the full `DEFAULT_ONLY` event set with desktop notifications intact.

That last part is a considered rejection of the obvious symmetry. A `pipeline_failed` event on an excluded MR means somebody pushed and a *new* pipeline ran — it is the one signal that can tell you the exclusion has gone stale while the row is suppressed, and it only fires on a transition, so a genuinely dormant branch emits nothing anyway. Dropping those events would buy no quiet and cost the staleness check. If the noise is unwanted, `unwatch:gitlab-mr:19509` is the existing, separate, visible way to say so — and the next `radar` will report the MR as `UNWATCHED` on its exclusion line rather than claiming a coverage it does not have.

**Why not classify infra failures automatically?** Because a classifier that decides "this red is not your fault" is wrong in the confident direction, silently, for every MR, with no audit trail — the [#445](https://github.com/Digital-Process-Tools/claude-supertool/issues/445) defect with more reach. An exclusion is at least *declared*: it names an iid, carries a reason, sits in a file under review, and prints itself on every board. And half of !19509's red is a merge conflict against `master` that no trace-scanner can classify — it is a genuine problem the operator has chosen not to fix, which is a decision, not a detection.

## Radar tiers — everything is a tier, nothing is a default ([#528](https://github.com/Digital-Process-Tools/claude-supertool/issues/528))

Radar's core is one sentence, and merge requests are not in it:

> Reconcile registered tiers against live truth, heal their watchers, report, stay idempotent, and never render an unknown as green.

The GitLab MR board is simply the first tier anyone wrote. Since [#528](https://github.com/Digital-Process-Tools/claude-supertool/issues/528) it is registered by name like any other, and radar with nothing registered does nothing:

```json
{
  "ops": {
    "radar": {
      "radar_tiers": {
        "gl-mrs": {},
        "gl-runners": { "window": 1800, "quiet_when_healthy": true }
      }
    }
  }
}
```

The key merges into the `radar` op the same way `ops.gl-job.job_patterns` does, and reaches the preset as `SUPERTOOL_RADAR_TIERS`. Registration order is render order — put `gl-runners` first if you want the fleet verdict above the board.

### ⚠ Breaking change: `radar` now refuses until you configure it

Upgrading with no `radar_tiers` gets you this, on stderr, exit 1:

```
radar: no tiers configured. Add ops.radar.radar_tiers to .supertool.json —
       e.g. {"gl-mrs": {}} for the GitLab MR board.
```

**Migration is one line:** add `"radar_tiers": { "gl-mrs": {} }` to the `radar` op block above. Everything then behaves exactly as before — same board, same filters, same exclusions, same feed, same snapshots.

The break is deliberate, and so is the shape of it:

- **Not a silent no-op.** Silence is the failure this whole preset is built against. An unconfigured radar that prints nothing is byte-identical to a healthy one, which is [#486](https://github.com/Digital-Process-Tools/claude-supertool/issues/486) with the failure moved one level up.
- **Not a `gl-mrs` default.** That is an opinion imposed on strangers: it points GitLab API calls at people who may be on GitHub, and hides from them that radar is configurable at all.
- **The message is the documentation.** It teaches the config at the moment someone needs it — a loud break carrying its own fix, rather than a quiet one.

`radar_tiers: {}` is treated the same as absent. A config that watches nothing is not a board worth printing.

### The tier contract

A tier is a Python module reachable by name, exposing:

```python
RADAR_OPTIONS = {"window", "quiet_when_healthy"}   # validated; a typo is reported
RADAR_QUIET_DEFAULT = True                         # optional, default True

def radar_report(options=None):
    ...
    return ["radar: FLEET — 14 pending job(s) cannot start"], False
```

| Member | Meaning |
|---|---|
| `RADAR_OPTIONS` | config keys this tier understands. Anything else in its block is **named on the board**, never silently ignored — a dropped option is how someone comes to believe they configured a threshold they did not |
| `RADAR_QUIET_DEFAULT` | is a healthy tier silent? `True` for a side concern like the runner fleet. `False` for a tier whose report *is* the board — an MR reconcile that prints nothing on a quiet day is indistinguishable from one that failed to run. Overridable per-tier with `quiet_when_healthy` |
| `radar_report(options)` | `(lines, healthy)`. **`healthy` means "this tier could tell you the truth"**, not "the world is fine". A board full of red MRs is a healthy report; a board that could not be built is not |

Radar injects two reserved keys into `options` before the call. Config cannot set them — any key starting with `_` is refused and reported — so a tier can trust them:

| Key | Meaning |
|---|---|
| `_arg` | the raw invocation argument. `radar:author=@me` arrives as `"author=@me"`; a bare `radar` as `""` |
| `_watch` | `callable(source, scope, only=None) -> "alive" \| "spawned" \| "failed" \| "capped"`. Radar's bounded spawner: idempotent slot claim before the fork ([#476](https://github.com/Digital-Process-Tools/claude-supertool/issues/476)) and the [#513](https://github.com/Digital-Process-Tools/claude-supertool/issues/513) death cap. Every slot a tier asks for is recorded, and radar itself emits the cap warning when one is refused |

**Why `_watch` is a callable and not a `radar_watchers()` list.** A declared slot has to be spawned *before* the report runs, and the MR tier must **not** spawn its discovery feed when live GitLab was unreachable — nothing should be spawned, pruned, healed or snapshotted on a population we could not read. Only the tier knows whether spawning is safe, and it needs the spawn result *inside* its own report, because the feed's status is a token in the board footer. Two mechanisms for one job is the drift this codebase keeps filing bugs about, so there is one: **radar owns the bound, the tier owns the timing.**

### Where a tier lives

Names resolve in two places, in order, and neither is a table that can drift when a file moves:

1. **`presets/watch/tiers/<name>.py`** (dashes as underscores). For a tier that needs radar's own internals — the transport, the dispatcher, the shared watch defaults. `gl-mrs` is here: `presets/gitlab/mrs.py` is a GitLab preset and the watch preset already depends on it, so putting reconcile machinery there would make the dependency mutual.
2. **the script the preset's op declares.** Any op joins by exposing `radar_report`. `gl-runners` is here — its report needs nothing but its own API helpers.

### Silence rules, and the third state

- a **healthy** tier says nothing unless its `RADAR_QUIET_DEFAULT` says otherwise — a green line per tier per run is what trains people to skim past the red one
- an **unhealthy** tier speaks on **every** run, never delta-suppressed, because it is a current fact rather than a transition
- a **misconfigured** tier always speaks, since that output is the only route to getting it fixed
- a tier that **raises**, or that cannot be resolved, is caught and named **on stderr, with exit 1** — never folded into the board. One broken tier must not be able to cost another its board, and it must not be able to leave radar exiting 0 either

That last rule is the house discipline made explicit: **three states, not two** — `ok`, a finding, and *cannot tell*. A tier that failed to load, a watcher that never spawned, a feed that is down — each renders as **unknown**, never as green. Catching an exception to keep radar rendering is right; catching it and rendering green is the same defect the fix was for.


## Bundled sources

| Source | Polls | Events |
|---|---|---|
| `github-pr` | `gh pr view <N> --json state,mergeable,reviewDecision,statusCheckRollup,comments,...` | `checks_failed`, `checks_succeeded`, `checks_pending`, `review_approved`, `review_changes_requested`, `comment_added`, `merged`, `closed`, `conflicts_appeared` |
| `gitlab-mr` | `glab api projects/:id/merge_requests/<iid>` | `pipeline_failed`, `pipeline_succeeded`, `pipeline_running`, `merged`, `closed`, `conflicts_appeared` |
| `gitlab-mr-feed` | `glab mr list` for a whole filter | `mr_opened`, `mr_merged`, `mr_closed`, `mr_left_feed` |
| `gl-runners` | `glab api projects/:id/runners` + the pending/running job queue | `runner_silent`, `runner_recovered`, `runner_starved`, `queue_cleared`, `runner_paused`, `runner_added`, `runner_vanished` |

Each source declares its event vocabulary in `presets/watch/sources/<NAME>/events.json` for introspection.

## What can be watched, and what cannot

A watcher is worth writing when **state changes without you, and finding out late costs something**. That is the whole test, and it is narrower than "the op returns data".

An op qualifies when all four hold:

1. **The state lives somewhere else.** A remote service, another machine, another person's action.
2. **It changes on its own timeline.** Nothing you type causes the transition.
3. **The change has a moment.** There is a before and an after worth naming, not a continuously drifting number.
4. **Learning late has a cost.** A pipeline that failed 40 minutes ago, an MR that picked up a conflict, a runner that stopped taking work.

| Watchable | Why | Status |
|---|---|---|
| `gl-pipeline` | a run transitions to success/failed while you do something else | shipped |
| `gitlab-mr` / `github-pr` | pipelines, reviews, conflicts, merges — all moved by other people | shipped |
| `gl-mrs` (population) | MRs open after your session started | shipped as `gitlab-mr-feed` |
| `gl-runners` | a runner stops taking work; GitLab keeps calling it `online` | shipped |
| `gl-issue` / `gh-issue` | labels, assignment and comments change; agent workflows key off labels | not yet |
| `gh-run` | the GitHub-side mirror of `gl-pipeline` | not yet |
| `devto_comments` / `bluesky` | replies and reactions arrive from strangers | not yet |

| Not watchable | Why |
|---|---|
| `read`, `grep`, `glob`, `ls`, `tail`, `map`, `between`, `tree` | pure functions of local files — the answer only changes when you change the file |
| `edit`, `replace`, `paste`, `vim`, `replace_lines` | synchronous mutations you initiated; the result is already in the return value |
| `phpstan`, `phpunit`, `rector`, `validate`, `format` | local analysis. Nothing transitions while you wait; just run it again |
| `git-status`, `git-diff`, `git-blame` | local repo state, moved by you |
| `mysql_read` | technically remote, but there is no transition worth naming — polling a table is a cron job, not a watcher |
| `mr`, `gl-issue-create` | one-shot actions. What they create may be watchable; the act is not |

The trap is criterion 3. Plenty of remote state changes constantly without producing an *event* — a row count, a queue depth, a token balance. Watching those yields a stream with no edges, and a signal that fires continuously is one people learn to ignore. If you cannot name the before and the after in a sentence, it is a metric, not an event.

Criterion 4 has its own trap, learned the expensive way: see `gl-runners` below, where a first version keyed on a field that looked like a transition and fired on the entire healthy fleet.

### `gl-runners` — silence is only news when work is stuck behind it

The op exists because GitLab reports a wedged runner as `status: online, job_execution_status: idle`, which is byte-identical to a healthy runner between jobs. Work pinned to that runner's exclusive tag queues behind it, no other runner is permitted to take it, and nothing turns red.

Liveness is judged on evidence in **descending strength**, and the order is load-bearing:

1. **jobs completed in the throughput window** — the runner demonstrably took work and returned results
2. **`job_execution_status == "active"`**, or the runner owning a job in `scope[]=running`
3. **`contacted_at` age**, last, at 30 minutes

Step 3 is last because **GitLab throttles `contacted_at` writes**. Measured on a live fleet: one runner's `contacted_at` stayed frozen at the same millisecond across 7 samples spanning 2 minutes, drifting to ~10 minutes of apparent staleness while the fleet completed jobs throughout. A first version keyed on it alone, at 5 minutes, and fired `runner_silent` on **6 of 6 online runners within the hour**. A signal that fires on the whole healthy fleet does not merely fail to inform — it buries the one event that was real.

So `runner_silent` gates on consequence: not taking work **and** work queued for it. A quiet runner with an empty queue has nothing to do, which is not a fault. `runner_starved` is the same correlation at the queue level, and it is the signal that earned its keep — it found 14 jobs pinned behind a runner GitLab had advertised as online for an hour.

Job history is filtered by `finished_at`, never `created_at`. Ids order by creation and the two are **not monotonic**: a test job created hours ago finishes after jobs created since, so scanning by creation drops exactly the long jobs whose completion is the best evidence.

**Steps 1 and 2 are evidence somebody has to go and gather, so the judgement refuses to run without it** ([#533](https://github.com/Digital-Process-Tools/claude-supertool/issues/533)). `annotate_recent_work` and `annotate_live_jobs` fold the throughput and running-jobs reads onto the runner records; a caller that skips them leaves `_is_responsive` holding step 3 alone — which is the version that fired on 6 of 6. So each annotator now leaves a mark, and an un-annotated record raises `UnannotatedFleetError` rather than receiving a verdict. Zero completed jobs is an observation; a missing `_recent_jobs` key is the absence of one, and the two must not read alike.

Refusing is the only answer that is not a lie in one direction or the other. Judging anyway re-ships the fleet-wide false alarm; defaulting to responsive reports an empty starvation list for a fleet nobody looked at, which is a **false all-clear in a tool whose whole job is to notice a wedge GitLab denies**. The layers above already carry a refusal correctly: the dispatcher records a failed poll as `last_error` and emits no events, and radar renders a raised tier as `WARNING — tier failed` with the board not green. Health UNKNOWN, said out loud.

The radar tier is the one caller that legitimately declines to gather the evidence — with an empty queue there is no starvation question, so the five-page history scan answers nothing and is skipped. It therefore no longer prints a live count in that case: `fleet ok — 10 runners, 0 pending, none blocked`, not a ratio inferred from the throttled field.

### `conflicts_appeared` is edge-triggered, and stays re-armable ([#463](https://github.com/Digital-Process-Tools/claude-supertool/issues/463))

`gitlab-mr` announced a standing conflict roughly once an hour with nothing resolved and nothing re-pushed — while `pipeline_failed` in the same poller fired once per pipeline. Both were written as rising edges. The difference is that GitLab computes mergeability **asynchronously**:

| `merge_status` | `has_conflicts` on a conflicted MR | |
|---|---|---|
| `cannot_be_merged` | `true` | settled — the answer |
| `can_be_merged` | `false` | settled — the answer |
| `unchecked` / `checking` / `cannot_be_merged_recheck` | `false` | **not computed yet** — not an answer |

Every push to the target branch puts every open MR back into `cannot_be_merged_recheck`, so the poller read "no conflicts", dropped its latch, and re-armed the edge for the next settled poll. Four pushes to `master`, four `conflicts_appeared` for one untouched conflict.

**The poller now only believes `has_conflicts` on a settled `merge_status`**, and carries the last known answer forward otherwise. Suppressing repeats outright would have been the opposite defect — a conflict that is genuinely resolved and later returns *must* fire again, and only a settled *clean* check releases the latch, so it does. A response with no `merge_status` field at all is not evidence of an unsettled check, so `has_conflicts` is still taken at face value there.

### `gitlab-mr` events carry the state that produced them ([#435](https://github.com/Digital-Process-Tools/claude-supertool/issues/435))

An event used to be unreadable on its own. `pipeline_succeeded` said which pipeline and gave you a URL, so answering "which branch, and is this MR still open?" cost a `gl-mr:<iid>:status` round-trip — for data the poller had fetched ~20s earlier and thrown away. Over one live radar session, four of six events triggered that confirm and not one of them changed a decision.

Every `gitlab-mr` event now carries eight extra payload fields. `_fetch` already returns the whole MR, so **this costs no additional API call**:

| Field | |
|---|---|
| `observed_at` | ISO-8601 `Z` — **when the API was read**. Always present. |
| `observed_mr_state` | `opened` / `merged` / `closed` |
| `observed_pipeline_status` | `running` / `failed` / `success` / … |
| `observed_pipeline_id` | head pipeline at the moment of the read — present on **every** event, including `merged` |
| `observed_pipeline_identity` | `same` / `new` / `unknown` — whether this poll could tell the head pipeline apart from the one the previous poll saw ([#537](https://github.com/Digital-Process-Tools/claude-supertool/issues/537), [below](#the-pipeline-edge-is-the-pipeline-not-the-status-string-537)). Not an MR fact: a fact about the read. |
| `observed_has_conflicts` | the poller's *corrected* flag, after the [#463](https://github.com/Digital-Process-Tools/claude-supertool/issues/463) carry-forward and the [#465](https://github.com/Digital-Process-Tools/claude-supertool/issues/465) empty-diff guard — **not** the raw `has_conflicts` |
| `observed_source_branch` | |
| `observed_target_branch` | |
| `observed_head_sha` | `""` on an MR with no commits |

#### What their staleness means, and how to read it

**These fields are a snapshot, never a live read.** They describe the state at `observed_at`, and the world may have moved since. That is the whole hazard: a value that reads as authoritative while being an artefact of when the tool happened to look is this repository's most-filed defect class, and a payload field holding a 20-second-old `mr_state` is exactly its shape. The fields ship only because their age is legible in the payload itself, by two deliberate choices:

- **The `observed_` prefix is on every field rather than on a wrapper object**, so the tense survives to the read site. There is no bare `payload["mr_state"]` to pluck, and no destructuring or logging helper can produce one. (Flat also because `notifiers/claude-channel` stringifies each payload key into an XML attribute — a nested `observed: {...}` would arrive as `[object Object]`.)
- **`observed_at` is an instant, not an age.** An `age_s` field is right for one second and quietly wrong from the next, which is the failure being designed against. A timestamp cannot go stale; subtract it yourself.

Note `observed_at` is **not** the record's envelope `ts`: `ts` is when the event was *emitted*, `observed_at` is when the data was *read*. Within a tick they differ by milliseconds, but only one is a claim about the data.

**How old can it be?** At most one poll interval, since the event is emitted by the tick that did the read — `INTERVAL = 30` for `gitlab-mr`. Which gives the practical rule: **an event less than `INTERVAL` old cannot be improved on by a confirm**, because the poller has not looked again either. Past that, decide by stakes — the snapshot is enough to render a board row or route a decision; confirm live before *advising* on a red, where being confidently wrong is expensive.

**The snapshot is consistent within a tick.** All events from one poll share one snapshot object, so a tick that emits both `pipeline_succeeded` and `merged` (observed live — the merge landed 20s before the poll ran) cannot have the two disagree about what was seen. This closes the divergence that made the confirm rule necessary: it was between the event *key* and the full state, not between read time and emit time.

#### What is not in the payload, on purpose

A fat payload that grows without bound is the real cost of this feature, so the set is fixed at eight fields (~200 bytes) and none of them scale with the MR.

| Excluded | Why |
|---|---|
| ~~failing-job ids on `pipeline_failed`~~ | **shipped in [#509](https://github.com/Digital-Process-Tools/claude-supertool/issues/509)** — see [below](#pipeline_failed-names-the-jobs-that-broke-509). The names, not the ids, and on a transition rather than per tick. |
| the job trace | kilobytes on every event, wanted only when someone actually classifies |
| `title`, `url` | already top-level in `payload` — duplication, not information |
| `user_notes_count` | a raw running total, so publishing it invites a "N comments" render of a number that is not the number of new ones. `comment_added` ships the **delta** as `new_count` instead. (This row used to say the field counts system notes. It does not — see [#519](https://github.com/Digital-Process-Tools/claude-supertool/issues/519) below.) |
| `merge_status`, `detailed_merge_status` | inputs to the conflict guard; its *output* is `observed_has_conflicts` |
| `description`, `labels`, `author`, `diff_refs` | grow with the MR |

#### Compatibility

No existing field was added, removed or retyped, and no event key changed meaning — the [#439](https://github.com/Digital-Process-Tools/claude-supertool/issues/439)/[#464](https://github.com/Digital-Process-Tools/claude-supertool/issues/464) invariant. `only=` filters name event keys and `events.json` is untouched, so no consumer's coverage moved. A consumer ignoring the new keys receives exactly what it received before, and is uninformed rather than wrong.

**`merged` deliberately gains no top-level `pipeline_id`**, though the tie-a-merge-to-its-pipeline gap it closes is real. `radar.drift()` reads `payload.pipeline_id` to decide an event is stale history superseded by a newer pipeline; a merge event joining that comparison would put `[drift: A→B]` on the board for something nobody reported. And the key's meaning would then depend on the event: "the pipeline this event is about" on `pipeline_*`, "the head pipeline at the time" on `merged`. `observed_pipeline_id` says the second thing, uniformly, everywhere.

### The request budget: what costs a call, and when

Two changes bought event self-sufficiency, and they were priced together on purpose because the whole question is *where* a call lands, not how many exist.

| | Extra GitLab requests | When |
|---|---|---|
| every `gitlab-mr` poll | **0** | the snapshot ([#435](https://github.com/Digital-Process-Tools/claude-supertool/issues/435)) and `comment_added` ([#519](https://github.com/Digital-Process-Tools/claude-supertool/issues/519)) are both answered by the one `_fetch` the poller already makes |
| transition **into** `pipeline_failed` | **1** | the failing-job lookup ([#509](https://github.com/Digital-Process-Tools/claude-supertool/issues/509)) |
| every other transition — `pipeline_succeeded`, `pipeline_running`, `merged`, `closed`, `conflicts_appeared`, `comment_added` | **0** | |

**Nothing was added to the per-poll path.** That was the design constraint: a watcher fleet is one process per open MR polling every 30s forever, so a request added there is multiplied by every MR you have open, all day. A request added to a failure transition is paid once per pipeline going red.

`comment_added` was expected to cost the per-poll kind — [#519](https://github.com/Digital-Process-Tools/claude-supertool/issues/519) proposed a `/notes?system=false` call on every poll of every watched MR — and on inspection it needed no call at all. See below.

### `pipeline_failed` names the jobs that broke ([#509](https://github.com/Digital-Process-Tools/claude-supertool/issues/509))

`pipeline_failed` used to say "pipeline 154253 failed" and stop there. Turning that into the name of the job that actually broke cost the consumer three round-trips — `gl-mr` → `gl-pipeline:<id>:failed` → `gl-job` — for one string, at the one moment a radar session is doing work and can least afford to spend context on it. And the job name *is* the failure class: `test_unit_dpt` versus `phpstan` versus `rector` is what tells a reader whether this is theirs to fix.

Three extra keys, on `pipeline_failed` **and no other event**:

| Field | |
|---|---|
| `observed_failed_jobs` | comma-joined job names, earliest-started first, capped at 5. `""` when there are none *or* when the lookup failed — read `observed_failed_jobs_lookup` to tell which. Over the cap, the string ends `,+N more`. |
| `observed_failed_job_count` | the true total as a string, e.g. `"8"` even when only 5 are named. **`""`, never `"0"`, when the lookup failed.** |
| `observed_failed_jobs_lookup` | `ok` — GitLab answered, the other two fields are its answer. `unavailable` — the request failed, timed out, returned something unparseable, or was never made (no pipeline id). |

They are absent, not blank, on `merged` / `comment_added` / the rest: a `merged` event has no failing-job concept, and three empty attributes riding on every event is wire noise that also invites a blank to be read as a fact.

#### Three states, because the lookup can fail

**A failed request must never render as "no jobs failed."** An absence produced by the tool read as an absence in the world is this repository's most-filed defect, and a red pipeline reporting zero failing jobs because a request fell over is its exact shape. So the answer is three-valued in the [#406](https://github.com/Digital-Process-Tools/claude-supertool/issues/406) / `docs/validators.md` vocabulary — `ok`, a finding, **cannot tell**:

| Situation | `lookup` | `count` | `jobs` |
|---|---|---|---|
| two jobs failed | `ok` | `"2"` | `"rector,test_unit_dpt"` |
| GitLab answered, recorded nothing pipeline-failing | `ok` | `"0"` | `""` |
| request failed / timed out / no pipeline id | `unavailable` | `""` | `""` |

The empty-string count is deliberate redundancy: a consumer that reads only `observed_failed_job_count` and never branches on `lookup` still cannot mistake "could not look" for "nothing broke". `subprocess.TimeoutExpired` is caught alongside `OSError` in the poller's `_glab_api` for the same reason — it is a `SubprocessError`, not an `OSError`, so it used to propagate out of `poll()` and kill the tick outright.

#### Why a list, and in that order

**No single job is elected "the" failure.** The tempting design is to name the first one, but the live data does not support it: on one observed pipeline eight `test_unit_*` jobs failed within **2.4 seconds** of each other. Picking one of eight parallel fan-out failures is arbitrary *and* reads as authoritative — the house defect again, in a field that looks like a fact. So a bounded list ships, and the truncation marker lives **inside** the joined string rather than only in the count, because a surface rendering that one attribute would otherwise read five names as the whole story.

The ordering is **ascending `started_at`**, jobs that never started last. The two alternatives were tested against the live API rather than reasoned about:

| Candidate | Verdict |
|---|---|
| GitLab's own response order | **Rejected.** It is descending job id — the API hands back the *last* failure first. Pipeline 154599 returns `test_unit_dpt` (started `:18.595`) ahead of `test_unit_modular` (`:15.216`). |
| ascending job id, as a proxy for stage order | **Rejected.** Job ids are not allocated in the order stages run. Pipeline 154527 gives `conformity_basic` the *lower* id (6953208) than the `unit` jobs (6953222+) while running it six minutes *later*. |
| ascending `started_at` | **Chosen.** Total over failed jobs, deterministic, and means what a reader assumes. |

Start order is chronology, **not causality** — jobs that fail in parallel are not a cascade, and the list makes no claim about which broke which.

Two filters, both from live data:

- **`allow_failure: true` jobs are dropped.** They fail without making the pipeline red, so naming one sends the reader to the wrong log. On pipeline 154527 the allow-failure job sorts *first* by every candidate ordering, so it is exactly the name that would have gone on the wire.
- **`status == "failed"` is re-checked** even though the query asks for it. `?scope[]=failed` is a request, not a guarantee, and an unfiltered response would otherwise turn every green job into a reported failure.

Retried jobs keep their name and take a new id, so both attempts come back failed; names are de-duplicated, because the reader wants the set of broken things and not a tally of attempts at one of them.

**On cost:** the request is `?scope[]=failed`, not the full job list. Real pipelines here run 114–139 jobs — two paginated pages of mostly `created`/`manual` bulk — against one short page for the scoped query. And it is issued from *inside* the `pipeline_status == "failed"` transition branch, which is edge-triggered, so a pipeline that sits red for an hour is looked up once rather than 120 times.

### The pipeline edge is the pipeline, not the status string ([#537](https://github.com/Digital-Process-Tools/claude-supertool/issues/537))

The pipeline events edge-trigger, and that is deliberate — a pipeline sitting red for an hour is announced once, which is what keeps a long-lived radar session from filling with repeats. But the edge used to be computed from the status **string** alone, with no pipeline identity in the comparison.

So a **second pipeline that also ended `failed`, with no `running` tick observed in between, fired nothing.** The previous status was already `failed`, the inequality was False, and the MR was red for a new reason in silence. The window is narrow at a 30-second poll — the whole run has to land between two polls — but a lint or conformity stage failing fast, or any poller restart that skips the intervening `running`, is enough. Observed live: MR !33194 ran pipeline 154628 to `failed`, took a push, and ran 154636 to `failed` as well.

And it undercut [#509](https://github.com/Digital-Process-Tools/claude-supertool/issues/509) exactly where that feature is worth most: an event that never fires is a set of failing job names that never arrives, for the second failure — the one where *"same breakage or a new one?"* is the actual question.

The edge is now the pair **(status, which pipeline)**. A change in either is a transition; a repeat of both is not.

#### Why this cannot double-fire a retry

The obvious risk is trading a silent miss for a duplicate, and duplicates are what train a reader to stop reading events. It does not happen, and the reason was settled against the live API rather than the docs:

**GitLab does not mint a new pipeline id when a job is retried.** Pipeline 154635 was caught mid-retry — `test_unit_pavillon` failed as job 6966698, was retried as job 6967497, and `head_pipeline` went on reporting id **154635** with its status flipped back to `running`.

| | pipeline id | status | |
|---|---|---|---|
| retry of a job | **unchanged** | `failed → running → failed` | already a status edge; the id comparison adds nothing |
| new push / trigger | **new** | may be `failed → failed` | the case that used to be silent |

So a retry moves the status under a stable id, which is the edge the old code already computed correctly. `test_a_retried_pipeline_is_not_double_announced` drives `failed → running → failed` under one id with repeat polls at every step and pins two events, and it passed before this change as well as after.

`pipeline_running` is deliberately left alone: a new pipeline starting while the previous one was still running stays quiet. It is not in `DEFAULT_ONLY` because you just pushed and it carries no information, and a second one carries no more.

#### Three states, because the identity can be unreadable

`unknown` is the third value, and it exists for the same reason `observed_failed_jobs_lookup` has an `unavailable`: **a failure to determine the identity must not resolve to "same pipeline, stay quiet"** — that is the silence this fix is about, reintroduced one level down.

| Situation | `observed_pipeline_identity` | |
|---|---|---|
| id matches the last one read | `same` | edge falls back to the status, as before |
| id differs, or it is the first pipeline seen for this MR | `new` | transition |
| no id in the payload, or a state file written before this field existed | `unknown` | **announced once**, marked, not folded into silence |

`unknown` fires **once per streak**, not once per poll — announcing it every tick would trade the silent failure for a flood, and a flood gets muted, which is the silence again by a longer route. It is re-armable: the poller carries the last id it could actually *read* forward across polls that reported none, the same shape the [#463](https://github.com/Digital-Process-Tools/claude-supertool/issues/463) unsettled-conflict check uses, so a readable id later is still comparable and a genuinely new pipeline is still announced.

**Cost: zero extra requests.** The id was already fetched, already in the snapshot, and already persisted in the state file that `radar.drift()` reads. The transition test simply did not consult it.

### `comment_added` is in the default set ([#519](https://github.com/Digital-Process-Tools/claude-supertool/issues/519))

**`user_notes_count` counts human comments only. It does not count system notes.** This is the correction of a claim that lived in a source comment, was never checked, was copied into the docs and into [#417](https://github.com/Digital-Process-Tools/claude-supertool/issues/417) item 3, and from there into [#519](https://github.com/Digital-Process-Tools/claude-supertool/issues/519) — which proposed one `/notes?system=false` request per poll per watched MR to fix a defect that does not exist.

GitLab scopes that counter over `Note.user`, which is `where(system: false)`. Re-derived against a live instance (GitLab 18.11.7) across twelve merge requests, where `user_notes_count` equalled the number of `system: false` notes every time:

| MR | system notes | human notes | `user_notes_count` |
|---|---|---|---|
| !19509 | 75 | 0 | **0** |
| !22026 | 20 | 2 | **2** |
| !33244 | 26 | 16 | **16** |
| !33265 | 3 | 0 | **0** |

!19509 is the decisive one: seventy-five system notes — pipeline activity, approvals, label and assignee edits, time tracking — and a count of zero. The predicted double-fire on every pipeline transition cannot happen.

So `comment_added` was excluded for one stated reason, that reason was not true, and it is now in `DEFAULT_ONLY`. A comment on your MR is actionable and otherwise silent, which is the same argument that puts `conflicts_appeared` there. **This costs no API call**: the count is already on the MR body the poller fetches every tick.

**What the count genuinely cannot do is say who commented**, so `comment_added` also fires on your own comments. That is a real limitation and it is not the one the event was held back for; it is cheap to live with and expensive to fix, since distinguishing authors *would* need the per-poll `/notes` call [#519](https://github.com/Digital-Process-Tools/claude-supertool/issues/519) costed. If it bothers you, drop `comment_added` from `only=`.

`new_count` is the **delta** since the previous poll, not the running total. A count that goes *down* (a deleted comment) fires nothing — the guard is a rising edge — and the first poll of an MR records a baseline without firing, so joining a conversation already in progress does not announce every comment in it.

### `conflicts_appeared` requires a diff ([#465](https://github.com/Digital-Process-Tools/claude-supertool/issues/465))

**`conflicts_appeared` is never emitted for an MR with no diff.** !33223 fired one second after being opened, with zero commits, `changes: 0` and `sha: null` — and the event was reported onward as a real conflict, with a false explanation built on top of it. A false `conflicts_appeared` does not read as noise, it reads as a fact.

`has_conflicts` is not a conflict field. GitLab's API entity exposes it as an alias for `cannot_be_merged?`, with this comment:

> `#cannot_be_merged?` is generally indicative of conflicts, and is set via `MergeRequests::MergeabilityCheckService`. However, it can also indicate that either `#has_no_commits?` or `#branch_missing?` are true.

So it over-reports in exactly one situation — an MR with no diff — and a conflict, which is two sets of changes overlapping, cannot exist without one. The poller clears it on **positive evidence of an empty diff**:

| Signal | Observed |
|---|---|
| `detailed_merge_status: commits_status` | !33194 — GitLab's own identifier for "source branch exists and contains commits" |
| `sha` present and null | !33223, the MR in the report |
| `diff_refs.head_sha` null, or `== base_sha` | source branch tip *is* the merge base — it carries nothing the target lacks |

**Absent fields are not evidence.** A payload with no `sha`/`diff_refs` leaves `has_conflicts` trusted, so the guard cannot argue itself into staying quiet about a conflict it merely failed to observe.

**`detailed_merge_status == "conflict"` is deliberately *not* the gate**, though it looks like the obvious one. It reports only the first failing mergeability check, and in `MergeRequest.all_mergeability_checks` the draft check runs second while the conflict check runs last:

| Blocked by | `detailed_merge_status` | Genuinely conflicted? |
|---|---|---|
| draft | `draft_status` | **possibly — the conflict check never ran** |
| unresolved threads | `discussions_not_resolved` | **possibly** |
| conflict, nothing else | `conflict` | yes |

Requiring `conflict` would silently stop reporting conflicts on every draft and every MR with open threads — the silent-omission class, strictly worse than the false positive. An allow-list of not-a-conflict reasons fails the same way, since it would need `draft_status` in it and `draft_status` precludes nothing. And the false positive is not draft-specific in the first place: !33194 was not a draft.

`radar` and `gl-mrs` render a `[conflict]` flag from the same field and carry the same false positive on a lower-stakes surface (a table column beside the row's other facts, not a lone event).

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
  },
  "first_tick": false
}
```

Consumers can rely on `ts/source/id/event/payload/first_tick` always being present. Extra fields inside `payload` vary by source — see each source's `events.json` and `poller.py`. `gitlab-mr` payloads additionally carry an [`observed_*` snapshot](#gitlab-mr-events-carry-the-state-that-produced-them-435) of the state that produced the event, timestamped so its age is readable without a call back.

### `first_tick` — the contract moved once, deliberately ([#464](https://github.com/Digital-Process-Tools/claude-supertool/issues/464))

**`first_tick: true` means the watcher emitted this on its first poll: the state it *found*, not a change it *observed*.** The state may be days old. `false` means a transition this watcher actually watched happen.

Pollers emit on first tick on purpose — it is how a new watcher announces an already-red MR, and it is what [#430](https://github.com/Digital-Process-Tools/claude-supertool/issues/430)/[#434](https://github.com/Digital-Process-Tools/claude-supertool/issues/434) are built on. The defect was only that a bootstrap emission and a live one arrived in the same shape, so a filter change that respawned watchers reported week-old pipelines as news. The fix makes the difference visible; it does not remove the emission.

It is keyed on **state, not process age**: a poller restarted with its state file intact is resuming, not bootstrapping, and marks nothing. Only a watcher with no prior knowledge — first spawn, cleared `/tmp`, a `radar` heal — has a first tick.

**What a consumer must tolerate.** The record gained one key. A consumer that ignores it receives exactly what it received before — same `event`, same `payload`, same `ts` — and is uninformed rather than wrong, which is the bar any addition here has to clear. Concretely:

| Guarantee | |
|---|---|
| **The locked `payload` did not move** | `first_tick` sits beside `ts`/`source`/`id`/`event`, because it describes the *emission* the way `ts` does, not the thing that happened. No source's payload gained, lost or retyped a field, and every key in every `events.json` still means what it did — the same claim [#439](https://github.com/Digital-Process-Tools/claude-supertool/issues/439) made, kept honest by keeping the change out of `payload`. |
| **No event key changed meaning** | The alternative — a separate key for bootstrap state — would have silently withheld first-tick emissions from every consumer whose `only=` filter names the real keys, turning a cosmetic gap into the omission class. `pipeline_succeeded` on first tick is still `pipeline_succeeded`. |
| **Always present, never inferred** | Emitted as `true`/`false` on every record, so a consumer never has to decide what an absent key meant. On the `claude-channel` bridge the attribute *is* omitted when the poller did not send one — an old poller has not told us the event was live, and that is unknown, not false. |
| **Not a suppression** | Nothing is dropped, delayed or deduplicated. A consumer that wants the old firehose keeps it verbatim by ignoring the field. |

**The wire record itself has not moved.** What changed with [#434](https://github.com/Digital-Process-Tools/claude-supertool/issues/434) is *which* records get emitted, not their shape: no field was added, removed or retyped, and every event key in every `events.json` still means what it did. The contract that moved is the one **between the two GitLab tiers** — `gitlab-mr-feed` no longer emits `mr_merged`/`mr_closed` for an iid whose per-MR poller announces `merged`/`closed` itself, so a consumer that counted on receiving both keys for one merge now receives exactly one. Consumers wanting every terminal transition should keep treating `merged`/`mr_merged` (and `closed`/`mr_closed`) as the same fact under two keys — which is what they always were.

The status file gained one field, `only`: the event filter its poller was spawned with, as a list. `[]` means unfiltered (every event). Absent means the file was written by a poller that predates the field, and is not evidence that the poller emits everything.

## Lifecycle

Each `watch` invocation forks a detached poller process. The process IS the subscription — no central config:

- PID file per active watcher: `/tmp/supertool-watch-{source}__{id}.pid`
- `unwatch` stops **every** live poller for that slot — the tracked one and any untracked ones — SIGTERM then SIGKILL, and removes the file
- Stale PIDs swept by the `watches` op automatically
- Pollers auto-stop when the source declares the target terminal (`is_terminal(state) -> bool`)

`SOURCE` and `ID` must not contain `__` (reserved as the filename separator) or `/` (both are interpolated into a `/tmp` path).

### One poller per slot ([#476](https://github.com/Digital-Process-Tools/claude-supertool/issues/476))

`(SOURCE, ID)` is the slot, and it is a singleton. The PID file *is* the claim: it is created with `O_CREAT|O_EXCL` **before** the fork, by the process asking for the poller, so exactly one caller can win. Losing costs nothing — the loser has not bound anything, spawned anything or unlinked anything, so it just says so and stops. All three spawn tiers come through the same door (`dispatcher.start_poller()`): the `watch` op, `radar`'s feed, and `radar`'s per-MR heal.

The heal tier is the one where a race is most expensive, and it is worth saying why. `radar` heals from a `watched` set it computed a moment earlier from the pidfiles, which is the test-then-fork shape above; and since [#417](https://github.com/Digital-Process-Tools/claude-supertool/issues/417) widened the watched population from "the MRs that are already failing" to "every open MR", two overlapping radar runs duplicate the *whole fleet* rather than a single poller. A slot already held is reported neither as healed nor as uncovered — radar did not spawn it, so claiming the action would be false, but the MR is covered, and a spurious `[UNWATCHED]` is the one warning on the board that has to stay trustworthy.

The claim, not a `os.path.exists()` test, is what makes this work. The poller publishes its own PID only after a fork, an interpreter start and a detach, so anything that *reads* the PID file to decide whether to spawn is looking through a window several hundred milliseconds wide — which is how nine pollers over one filter accumulated in same-second groups.

A refused start prints what it found and which live process holds the slot:

```
$ ./supertool 'watch:gitlab-mr:33223'
Already watching gitlab-mr:33223 (PID 71163) — not starting a second. Use ./supertool 'unwatch:gitlab-mr:33223' to stop it.
```

It is never silent, and never rendered like a clean start. Exit status stays `0` — a refusal is the op working, not failing. A spawn that genuinely fails prints `ERROR: could not spawn a poller for …` and exits `1`.

Three rules follow from "a missing watcher is worse than a duplicate one":

- **A slot whose owner is dead is reclaimed**, after one retry. A crashed poller must not wedge its id shut forever; an unwatched population renders exactly like a quiet one.
- **A failed spawn releases the slot.** A claim left by a poller that never started would refuse every future start for that id.
- **A poller releases its PID file only if it still owns it.** One shutting down slowly, whose slot was meanwhile reclaimed, must not unlink its successor's claim on the way out.

For the feed tier the id is a *filter string*, so it is canonicalised (sorted keys, sorted deduped values) before it becomes a filename: `author=a,author=b` and `author=b,author=a` are one population and must be one poller. That merges only filters that are already the same set, so it can never refuse a filter that would have selected something different. Board labels still print the filter as you typed it.

This prevents duplicates from being created. It does **not** reap detached pollers left over from before — those are `PPID 1` and nothing will reap them; `unwatch` (or `kill`) is still the way out.

### A slot tracks a set of PIDs, not one ([#511](https://github.com/Digital-Process-Tools/claude-supertool/issues/511))

The claim above stops duplicates being *created*. It does nothing about the ones already running, and until #511 the tool could not even see them: the state model was one `{id → pid}` mapping, so a second poller on a slot was not merely untracked, it was **unreachable**. Observed over a five-hour session: `watches` showed one watcher while several emitted, one `mr_opened` arrived 3×, then 9×, then 13× in four seconds, `unwatch` reported `Stopped watcher … (PID 92379)` and the events continued, and the next `unwatch` said `No active watcher` while the state file was still being rewritten every tick.

So a slot is now read as a **set**, from two independent sources:

| source of truth | what it knows | what it misses |
| --- | --- | --- |
| the PID file | which poller *claimed* the slot | anything that did not claim it; whether the claimant is still alive |
| a `ps` scan for labelled pollers | every live poller that names this slot in its own argv | a poller spawned before the labelling landed (see below) |

`watches` renders the union. An id with more than one live poller shows its count and every PID; a poller whose PID file was deleted is listed as `no pidfile` instead of vanishing. `unwatch` acts on the union: it prints every PID with its provenance (`tracked` / `untracked`) **before** signalling anything, stops each one, names any it could not stop rather than aborting the rest, and exits `1` if any refused.

It is a multi-kill, and that is a deliberate trade. The failure it replaces is a survivor nobody can reach whose only recovery was `pkill`; the failure it risks is stopping a process someone wanted. The breadth is bounded by evidence rather than by a pattern: every PID it acts on belongs to a process whose own argv names this exact source and id **as whole tokens**, so `33248` cannot match `332480` and an id appearing inside some other command's arguments cannot be mistaken for a poller. What it can still over-reach on is a poller started from a *different checkout* of supertool — which shares the same `/tmp` slot, and so genuinely is the same watcher.

Three absences are now three different sentences, because they call for different actions:

- `No active watcher for … (no PID file, and no matching process)` — nothing is running, verified both ways.
- `Tracked PID N … is not running` — the slot recorded a poller that died with nothing reporting it, so this id has been **unwatched** since. #511 caught two of those, and the board was silently blind on both MRs.
- `… the process scan was unavailable` — `ps` could not be read, so an untracked poller could not be ruled out. Never rendered as "no watcher".

### `ps` cannot be used to identify a watcher

**Do not read `ps` output to decide which watcher a process is. Use `watches`.** When the two disagree, `watches` is right.

A poller is forked, so it inherits the argv of whatever spawned it. Every per-MR watcher therefore displayed the *feed's* command line:

```
19156 radar.py author=@me,author=modular.system,state=opened   <- actually the watcher for MR !33249
19158 radar.py author=@me,author=modular.system,state=opened   <- actually the watcher for MR !33248
```

In #511 those rows were read as duplicate feed pollers and two were killed. They were the watchers for two different MRs, one of them the MR that most needed watching.

Pollers spawned since #511 fix this at the source: after the fork the grandchild `exec`s into an argv that names itself, so the command line is not a label describing the process — it *is* the process, and it is the same argv the scan matches on, which is why `ps` and `watches` cannot drift apart:

```
26951 /usr/bin/python3 …/presets/watch/dispatcher.py poll gitlab-mr 19509
26968 /usr/bin/python3 …/presets/watch/dispatcher.py poll gitlab-mr-feed author=@me,state=opened
```

`exec` rather than `setproctitle`: no new dependency, and the PID is unchanged, so the claim taken before the fork and the PID reported up the pipe both stay valid. If the `exec` fails the poller runs anyway, unlabelled — a working poller that is hard to see beats no poller.

The limit, stated plainly: **a poller started before this landed still wears its parent's argv**, so neither the scan nor `unwatch` can find it, and nothing can tell it apart from the process that spawned it. Clearing those is a one-time `pkill -f 'presets/watch/'` followed by a fresh `radar`. Every poller started after it is reachable by `unwatch`.

### A watcher that died keeps saying so ([#513](https://github.com/Digital-Process-Tools/claude-supertool/issues/513))

`watches` used to unlink a dead poller's stale PID file and drop the row, so an id that **had** coverage and lost it rendered byte-identically to one that never had any. That is the worst member of this repository's recurring family, because the thing going quiet is the monitoring surface itself: a validator that declines costs one check, a radar that lost a watcher costs *every* event on that MR while the board keeps rendering as though coverage were complete. "Nothing to report" and "not watching any more" are the two states a monitoring surface most needs to keep apart.

`unwatch` had said it since #511 — but only if you happened to run it. The passive surface, the one a session actually reads, said nothing.

#### What tells a death from a deliberate stop

Not a heuristic — the artifact each exit leaves behind:

| exit | PID file | state file |
| --- | --- | --- |
| terminal (`merged` / `closed`) | released by the poller | **cleared** |
| deliberate `unwatch` | released | kept |
| death (SIGKILL, crash, OOM, reboot) | **left behind, naming a dead PID** | kept |

So **a PID file naming a dead process is a death, and nothing else is.** The poll loop releases the slot on a deliberate stop and on a terminal exit alike, and `unwatch` releases it too; what is left over is a poller that never ran its shutdown path.

The ledger is kept **inside the state file**, and that placement is the design rather than a convenience: a terminal exit deletes the state file, so a legitimate exit cannot leave a record for anything to misread as a loss. The invariant holds by construction, not by a check that could drift out of step with the poll loop.

Deaths are derived from the PID file **only**. The labelled-process scan never contributes evidence of one — a poller spawned before #511's labelling is invisible to the scan while being perfectly alive, and treating scan-invisibility as death would report the entire pre-existing fleet as lost on the first run after this landed.

#### What the board shows

| state | `watches` row | when it clears |
| --- | --- | --- |
| healthy | PID, started, last event | — |
| **lost** | `PID` column is `-`, note `LOST — PID N died, no poller since` | a poller covers the slot again, or `unwatch:SOURCE:ID` acknowledges it |
| **flapping** | live PID, note `flapping — N deaths recorded, currently respawned` | shown only above one death; a slot that died once and healed cleanly goes quiet |
| refused | absent from `watches` once re-covered; `radar` prints a standing WARNING | `watch:SOURCE:ID` re-arms it |

A LOST row is a supervision record, not a message that scrolls past once: it is printed on every `watches` until it is resolved or acknowledged.

#### `radar` heals it, but the healing is bounded

`radar.heal` reaps the stale PID file first, so the death is on record before the new claim overwrites the evidence, then respawns. Past **3** recorded deaths (`transport.DEATH_RESPAWN_LIMIT`) the slot is **refused** rather than respawned:

```
radar: WARNING — !33161 has lost its poller 3 times; NOT respawning. This MR is
unwatched until the cause is fixed and it is re-armed: ./supertool 'watch:gitlab-mr:33161'.
```

Respawning forever would keep the board green while a watcher failed over and over — a visible failure converted into an invisible loop, which is this same bug one level up. A refused slot is reported as **uncovered**, because it is: the automation has stopped, and the only thing worse than saying so is not saying so.

A loss that *was* healed prints one line, on the run that healed it, and then goes quiet:

```
radar: NOTE — !33161 lost its poller (PID 42520 died without being unwatched, 1 recorded); respawned.
```

The asymmetry is deliberate. A permanent warning on a slot that is now covered is what teaches a reader to skim the board, and skimming is how a real red gets missed — the failure mode [#511](https://github.com/Digital-Process-Tools/claude-supertool/issues/511) opens with. The same reasoning is why a deliberate `unwatch` clears the ledger: withdrawing coverage on purpose is not losing it.

#### Clearing a record

Nothing automatic clears it. Two operator actions do, and both mean "I have seen this":

- `unwatch:SOURCE:ID` — acknowledge and stop. Drops the row.
- `watch:SOURCE:ID` — acknowledge and re-arm. Clears the deaths, so `radar` will respawn the slot again if it dies.

**Known residual:** a slot that lost its poller and whose MR then merged or closed *without the dead poller ever observing it* keeps its LOST row, because nothing observed the terminal state that would have deleted the file. `radar` will not clear it either — absence from one board's filter is deliberately not treated as proof the MR is gone, for the same reason [`prune_terminal`](#radar--reconcile-dont-just-report) does not treat it that way. One `unwatch:SOURCE:ID` drops the row. The error direction is deliberate: over-reporting on a monitoring surface is recoverable, under-reporting is what this whole section is about.

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
