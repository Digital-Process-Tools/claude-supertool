# dashboard — "what do I do next", in one read-only call

`dashboard` is the join. Four reads used to answer the maintainer's standing question
and none of them answered it:

```bash
git fetch && git pull --ff-only            # is my clone current
gh run list --branch master --limit 2      # is the default branch green
supertool 'gh-prs:state=open'              # what is on the board
supertool 'git-worktrees'                  # which trees are occupied
```

Each returns something true. The decision lives in joining them, plus two facts
none of them returned at all — **which PRs are mergeable right now**, and **which
lanes are free**. That join was performed by hand, six times in one session,
which is what filed [#953].

```
supertool 'dashboard'
```

Takes no arguments. Reads only. Ends on a one-line `[result]` that survives
`| tail -1`.

## What it prints

```
# dashboard — Digital-Process-Tools/claude-supertool

local
  branch fix/953 @ 4f1c9a2
  origin/master 29ca64c — matches the remote, clone is current
  HEAD is 3 ahead / 0 behind origin/master

default
  master 29ca64c — GREEN ...
  legs: 20 total: 20 passed, 0 failed, 0 pending

board
  UNKNOWN  #944   fix/924              containment    2 cancelled — neither a pass nor a failure ...
  WAITING  #951   fix/844-909-777      ci-cost        6 of 20 legs still moving
  MERGE    #947   fix/941              git-ops        20 of 20 legs passed, mergeable, no conflicts

worktrees
  occupied     /Users/f/Documents/st-wt/859   fix/859   watch
  cannot tell  /Users/f/Documents/st-wt/945   fix/945   lane ?
  'cannot tell' is NOT 'idle' — expand one with git-worktrees:<path>

lanes
  occupied   watch          #954 open (fix/859) · /Users/f/Documents/st-wt/859 occupied
  unknown    validators     /Users/f/Documents/st-wt/665 cannot tell — undecidable, so this lane declines ...
  free       release        no open PR and no live worktree points here
  'unknown' is NOT 'free' — an undecidable or unplaced occupancy could be in it

next: 1 PR is ready — review and merge it: gh-pr:947:diff

[result] dashboard: 1 UNKNOWN, 1 WAITING, 1 MERGE · 1 lanes free, 5 occupied, 1 unknown · 0 sections unread
```

## The verdict column

`MERGE` is an assertion a human acts on immediately, so it is the last branch
reached and the only one that asserts anything. In order:

| Verdict   | When                                                                         |
| --------- | ---------------------------------------------------------------------------- |
| `UNKNOWN` | the rollup never came back; **the tally does not sum**; zero checks; any leg in a state that is neither a pass, a failure nor pending; mergeability in a state this op will not read as mergeable |
| `RED`     | at least one leg failed                                                      |
| `REBASE`  | green, but conflicting, dirty, or behind the base                            |
| `WAITING` | legs still moving, or green but GitHub reports the merge `BLOCKED`           |
| `DRAFT`   | green, but the PR is a draft                                                 |
| `MERGE`   | every leg in the passed bucket, `MERGEABLE`, and `CLEAN`/`HAS_HOOKS`         |

Two things are load-bearing.

**The tally must sum.** The leg count is reconciled against the legs the run
declares using `gh-pr`'s own `_reconcile_checks` — `_checks.shortfall` over
`jobs?filter=all`, read off the commit rather than off the rollup ([#724],
[#804]). When it comes back `⚠ INCOMPLETE` or `⚠ TALLY UNVERIFIED`, the verdict
is `UNKNOWN`. Not `WAITING` — that asserts the legs are still coming — and never
`MERGE`, which is what "the legs I read all passed" would otherwise produce on a
PR whose other six legs were never in the payload.

**`MERGE` requires `_checks.all_green`, not the absence of a failure.**
`CANCELLED`, `SKIPPED`, `TIMED_OUT`, `NEUTRAL` and `ACTION_REQUIRED` are none of
them passes, and neither is a state GitHub adds next year. Enumerating the bad
states would always trail the platform; requiring every leg to land in the
`passed` bucket cannot.

## The lane vocabulary is configured, and unconfigured refuses

There is no default lane prefix, and adding one would be the bug ([#1007](https://github.com/Digital-Process-Tools/claude-supertool/issues/1007)). The prefix was hardcoded as `lane:` — read out of the *title* of [#964](https://github.com/Digital-Process-Tools/claude-supertool/issues/964), an issue whose entire subject is a colon that appears in no label name. It selected none of this repository's seven `lane-*` labels, so every lane resolved to unplaced and the whole section went inert. `claude-supertool` spells lanes `lane-watch`; `claude-remember` spells priorities `priority:high`. Same organisation, one repository apart, opposite convention: there is no literal that is right in both.

```json
{ "ops": { "dashboard": { "lane_prefix": "lane-" } } }
```

Spell it exactly as your labels do, including the separator. With the key absent the lanes section prints `!! unread`, names the key, and the footer reads `lanes UNCONFIGURED` — the same property `radar_tiers` has ([#528](https://github.com/Digital-Process-Tools/claude-supertool/issues/528)), for the same reason: a prefix nobody chose selects zero labels, and zero labels renders byte-identically to a repository with no work in flight.

**An empty lane universe is its own state.** A *configured* prefix that matches nothing is a different failure from a repository that genuinely declares no lanes, and `0 free, 0 occupied, 0 unknown` is the same sentence for both — which is why #1007 had to be found by grepping the source rather than by reading the op's own output. It now reports:

```
lanes
  !! degraded — no label matches 'lane:', so the lane universe is empty and no lane
     can be reported free. 7 label(s) share its stem behind a different separator
     (lane-ci-cost, lane-containment, lane-git-ops, lane-release, +3 more) — if the
     vocabulary here is 'lane-', set ops.dashboard.lane_prefix to it; nothing is
     assumed on your behalf

[result] dashboard: ... · lane universe EMPTY — no label matches 'lane:' · ...
```

The near-miss list is a suggestion the reader applies, never a fallback the op applies for them: silently retrying the other separator would rebuild exactly the guess this removes. When nothing resembles the prefix either, it says that instead — no lane is claimed free in either case.

## The lane column

Lane occupancy is **inferred**, and each hop can be absent.

* Lane labels live on **issues**. Measured on 2026-08-07, against the seven
  labels this repository actually declares (`lane-watch`, `lane-release`, …):
  54 of 65 open issues carry one, and **no open PR carried one at all**. A lane
  board built from PR labels would have printed all seven lanes free while six
  PRs and thirteen worktrees were live.
* A PR reaches its lane through its own labels and through
  `_checks.closing_issue_refs` on its body.
* A worktree reaches its lane through the PR on its branch, and through the
  issue numbers in the branch name (`fix/941` → 941). That second hop is a
  naming convention, not data, so it only ever *adds* a lane.

The rule, and the three states:

| Lane state | When                                                                       |
| ---------- | -------------------------------------------------------------------------- |
| `occupied` | an open PR maps to it, or a worktree `git-worktrees` calls `occupied`       |
| `unknown`  | its only signal is a worktree `git-worktrees` could not decide — `cannot tell` is not `idle` one layer up either; **or** some live worktree anywhere could not be attributed to any lane |
| `free`     | nothing points at it **and** every live occupancy in the repo was placed    |

The last clause is the one worth understanding. `feat/pr-ops` carries no issue
number, so nothing maps it to a lane. Printing another lane `free` beside an
occupancy nobody could place is a claim the data does not support — so every
otherwise-free lane degrades to `unknown` and names the stray. Reporting a lane
free while an agent is working in it is how two agents end up editing one file,
which is the failure the lane system exists to prevent.

An idle unattributed worktree does not deny `free`: it is not an occupancy.

**One exclusion, and only one.** The clone sitting on the repository's default
branch is not counted as a stray. Lane work happens on a `fix/NNN` branch in its
own worktree; the main clone sits on `master` permanently and is where the
symlinked binary lives, so counting it would deny `free` to every lane on every
call forever — and an alarm that can never clear is the same as no alarm. Every
other unattributable live tree still denies `free`, and the section names the
whole stray set once at the top rather than repeating it under each lane.

## Partial failure

Every section prints whether or not it has data. One that could not be fetched
renders its heading, `!! unread — <reason>`, and the sentence *"this section is
missing, not empty"*, and is counted in the `[result]` line. A dashboard missing its
board section must not be readable as a dashboard with an empty board — the
absence-produced-by-the-tool defect this repository files more than any other.

A section that *rendered* but from inputs that did not all arrive is a third
state again: it prints `!! degraded — <reason>` above its rows and is counted
separately in the footer (`0 sections unread, 1 degraded`). Two cases produce it:
the issue-label read failing — every lane then reads `unknown` because nothing
could be placed, not because anything was inspected, and `0 sections unread` on
its own would have said the opposite — and a configured `lane_prefix` selecting
no label at all, which is a finding about the vocabulary rather than a gap in
the read.

The lane footer therefore has four shapes, and none of them is a tally of
zeroes:

| Footer                                   | Means                                            |
| ---------------------------------------- | ------------------------------------------------ |
| `2 lanes free, 4 occupied, 1 unknown`    | the universe was read and has lanes in it        |
| `lanes UNREAD`                           | `gh label list` failed; nothing is claimed       |
| `lanes UNCONFIGURED`                     | no `ops.dashboard.lane_prefix`; nothing was tried |
| `lane universe EMPTY — no label matches` | the prefix ran and selected nothing              |

`next:` declines outright when the board is unread, because advice derived from
data the op never got is the most confidently wrong sentence it could print.

Exit code is 0 only when every section was read; 1 when any was not.

## Read-only, permanently

Nothing here spawns, heals, fetches or mutates:

* clone currency is `git ls-remote`, which reads the remote's ref without
  writing yours — so `dashboard` is safe to run from a worktree an agent is using,
  and it never performs half of the fix while answering whether one is needed;
* every command `next:` can name is a supertool **read** op — `gh-pr:N:diff`,
  `gh-pr:N:status`, `gh-job:N:fail`, `gh-check:N`, `gh-issues:label=…`;
* `tests/test_dashboard_953.py` pins that this file names no mutating verb.

A subsystem whose inspection was fused to its actions once stayed unobservable
for hours. That is not recreated here.

## Cost

One `gh pr list`, one `gh label list`, one `gh issue list`, one `gh repo view`,
one commit lookup, one run list, and then a fan-out: job lists per default-branch
workflow, and per open PR a run list plus up to four job lists for the leg
reconciliation. That is the price of the arithmetic behind `MERGE`, and it is
paid concurrently.

* `SUPERTOOL_DASHBOARD_BUDGET` — wall clock for the network reads, default `90`
  seconds. A section that runs out says so and is counted unread.
* `SUPERTOOL_DASHBOARD_WORKERS` — board fan-out width, default `8`.

## GitLab

Out of scope, and deliberately not half-built. `gl-mrs` answers the board half
for GitLab, but the lane vocabulary and the worktree join are GitHub-shaped
today, and a `dashboard` that silently covered three of five sections on GitLab would
be the same defect as an omitted section wearing a different hat.

## Related

`gh-prs` (the board alone) · `gh-branch` (the default branch alone) ·
`git-worktrees` (occupancy alone, with the full evidence per tree) ·
`radar`/`watch` (event-driven and continuous; `dashboard` is pull-based and one-shot).

[#953]: https://github.com/Digital-Process-Tools/claude-supertool/issues/953
[#724]: https://github.com/Digital-Process-Tools/claude-supertool/issues/724
[#804]: https://github.com/Digital-Process-Tools/claude-supertool/issues/804
