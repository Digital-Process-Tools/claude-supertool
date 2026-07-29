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

## Bundled sources

| Source | Polls | Events |
|---|---|---|
| `github-pr` | `gh pr view <N> --json state,mergeable,reviewDecision,statusCheckRollup,comments,...` | `checks_failed`, `checks_succeeded`, `checks_pending`, `review_approved`, `review_changes_requested`, `comment_added`, `merged`, `closed`, `conflicts_appeared` |
| `gitlab-mr` | `glab api projects/:id/merge_requests/<iid>` | `pipeline_failed`, `pipeline_succeeded`, `pipeline_running`, `merged`, `closed`, `conflicts_appeared` |
| `gitlab-mr-feed` | `glab mr list` for a whole filter | `mr_opened`, `mr_merged`, `mr_closed`, `mr_left_feed` |

Each source declares its event vocabulary in `presets/watch/sources/<NAME>/events.json` for introspection.

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
| failing-job ids on `pipeline_failed` | needs a second API call per failure tick — a real cost trade, scoped as a follow-up |
| the job trace | kilobytes on every event, wanted only when someone actually classifies |
| `title`, `url` | already top-level in `payload` — duplication, not information |
| `user_notes_count` | counts system notes (see below), so publishing it invites a wrong "N comments" render |
| `merge_status`, `detailed_merge_status` | inputs to the conflict guard; its *output* is `observed_has_conflicts` |
| `description`, `labels`, `author`, `diff_refs` | grow with the MR |

#### Compatibility

No existing field was added, removed or retyped, and no event key changed meaning — the [#439](https://github.com/Digital-Process-Tools/claude-supertool/issues/439)/[#464](https://github.com/Digital-Process-Tools/claude-supertool/issues/464) invariant. `only=` filters name event keys and `events.json` is untouched, so no consumer's coverage moved. A consumer ignoring the new keys receives exactly what it received before, and is uninformed rather than wrong.

**`merged` deliberately gains no top-level `pipeline_id`**, though the tie-a-merge-to-its-pipeline gap it closes is real. `radar.drift()` reads `payload.pipeline_id` to decide an event is stale history superseded by a newer pipeline; a merge event joining that comparison would put `[drift: A→B]` on the board for something nobody reported. And the key's meaning would then depend on the event: "the pipeline this event is about" on `pipeline_*`, "the head pipeline at the time" on `merged`. `observed_pipeline_id` says the second thing, uniformly, everywhere.

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
