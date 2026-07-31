# github

GitHub ops via the `gh` CLI. Replaces the 4-6 separate `gh` calls needed to review a PR (branch, checks, approvals, diff, comments), debug a failed Actions run, and manage social activity (follows, stars). The PR and issue ops pack a full dashboard into one call — no follow-up turn needed to decide whether to merge or where the failure is.

## Requires

[`gh` CLI](https://cli.github.com) installed and authenticated (`gh auth login`).

A GitHub-cloned cwd, **or** a `repo:OWNER/NAME` target — see [Targeting another repo](#targeting-another-repo).

## Ops

| Op | Syntax | What it returns |
|----|--------|-----------------|
| `gh-issue` | `gh-issue:NUMBER[:full]` | Issue metadata, description, all comments (truncated by default), linked PRs, image download. `:full` disables truncation |
| `gh-pr` | `gh-pr:NUMBER_OR_BRANCH[:status]` | Full PR dashboard: branch, checks, reviews/approval state, linked issue, diff stat, comments. `:status` returns slim merge-state plus the `head -> base` branch line only (~250 bytes). The check line opens with `N total:` and every count after it sums back to N, so a state the tally does not recognise is named (`2 cancelled`) instead of dropped; anything short of every-check-passed carries `⚠ NOT ALL GREEN`, and zero check runs renders as one of four states — `none yet`, `none, and none will be created`, `none until the conflict is resolved … Rebase`, or a stated `UNKNOWN` — rather than one sentence for all of them ([#585](https://github.com/Digital-Process-Tools/claude-supertool/issues/585), [#594](https://github.com/Digital-Process-Tools/claude-supertool/issues/594), see [Zero check runs](#zero-check-runs-is-four-states-not-one)). The linked issue is every issue a GitHub closing keyword binds to — all of them, plural label when there are several — and a stated `none declared` when the body names no keyword, never the first `#N` in the body ([#591](https://github.com/Digital-Process-Tools/claude-supertool/issues/591), see [The linked issue](#the-linked-issue-is-a-declared-closing-reference-not-the-first-n)) |
| `gh-prs` | `gh-prs[:author=@me,reviewer=@me,state=open,failed,nopipe,iids]` | PR triage board: your open PRs sorted failing-first then stalest. Per PR: check rollup (a failure shows the failing **check name**), approval state, age, diff size, watch-state, `draft`/`conflict`/`threads` flags + footer pointing at the first failing-and-unwatched PR. The gl-mrs twin. `iids` emits a bare number list for `watch-mine.sh` |
| `gh-run` | `gh-run:NUMBER` | Workflow run job list with statuses and failed step names |
| `gh-job` | `gh-job:NUMBER[:raw[:-N\|:START[:END]]\|:grep:PATTERN]` | Job failure detail: PR context + error pattern search + log tail. `:raw` dumps the full trace; `:raw:START:END` slices lines (1-indexed, inclusive); `:raw:-N` returns the **last N lines**, and a START past the end returns the tail of the width requested with a line saying so rather than declining — see [Reading a range](gitlab.md#reading-a-range) ([#487](https://github.com/Digital-Process-Tools/claude-supertool/issues/487)); `:grep:PATTERN` runs an ad-hoc regex over the log (literal fallback on bad regex, ±context, names the pattern + tail on no-match — never silent-empty). Optional per-job `job_patterns` table in `.supertool.json` (see gitlab preset doc) maps job names to tighter patterns + a `resolution` op. Zero matches on a job GitHub calls `failure` prints `## FAILED — no error pattern matched` — patterns tried + a log tail, never silence. `## No error patterns matched` survives only for jobs that did not fail |
| `repo:` prefix | `repo:OWNER/NAME` (leading op) | Points `gh-pr`, `gh-prs`, `gh-issue`, `gh-run`, `gh-job` at a repo other than the cwd's — see [Targeting another repo](#targeting-another-repo) |
| `gh-follow` | `gh-follow:USERNAME` | Follow a GitHub user via the authenticated session |
| `gh-following` | `gh-following[:N]` | List users you follow (default 30) |
| `gh-batch-follow` | `gh-batch-follow:FILE` | Follow each username from a file (one per line, `#` comments). 1s delay between calls |
| `gh-star` | `gh-star:OWNER/REPO` | Star a repository |
| `gh-starred` | `gh-starred[:N]` | List repos you have starred (default 30) |
| `gh-batch-star` | `gh-batch-star:FILE` | Star each `OWNER/REPO` from a file (one per line, `#` comments). 1s delay between calls |
| `gh-find-followable` | `gh-find-followable:OWNER/REPO[|N]` | Discover candidate users to follow: pulls stargazers + contributors, deduplicates, filters orgs. Pipe output to a file then review before `gh-batch-follow` |
| `gh-find-starable` | `gh-find-starable:TOPIC[|N]` | Discover repos worth starring by topic, sorted by stars. Pipe output to a file then review before `gh-batch-star` |

## Targeting another repo

Every read op above took its repo from the cwd's git remote and offered no override, so two repos were unreachable through the ops at once: any repo you have not cloned, and any repo whose work happens from a project root that is **not** a GitHub clone. `gh-issue-create` had accepted a `repo` key in its payload since it shipped, so the write side could already name a target while the read side could not ([#673](https://github.com/Digital-Process-Tools/claude-supertool/issues/673)).

A leading `repo:OWNER/NAME` op supplies it:

```bash
./supertool 'repo:Digital-Process-Tools/claude-remember' 'gh-pr:265:status'
./supertool 'cwd:~/projects/a-gitlab-repo' 'repo:some-org/a-github-repo' 'gh-run:30654362436'
```

| | Rule |
|---|---|
| Position | First op, or immediately after `cwd:` — `cwd:` keeps its own must-be-absolutely-first rule |
| Count | One per call |
| Shape | `OWNER/NAME`, validated before anything runs |
| Scope | The whole call. Two targets in one call is two calls |
| Accepted by | `gh-pr`, `gh-prs`, `gh-issue`, `gh-run`, `gh-job` |

**Why a leading op and not a trailing `…:repo=OWNER/NAME` token.** The suffix grammar in this family is not free. `gh-job:ID:grep:PATTERN` takes an arbitrary regex in that position, so `gh-job:5:grep:repo=x` is a legitimate log search that a trailing-token scan would silently steal — and `gh-prs` already spells its filters `key=value` *inside one comma-separated token* (`gh-prs:author=@me,state=open`), so a second, colon-separated `key=` grammar would be two rules for one idea. A leading op also lands in one place in the dispatcher instead of in five presets' argument parsers.

### A target no op can honour is refused

```
$ ./supertool 'repo:owner/name' 'gh-pr:1:status' 'read:foo.py'
repo: 'read' cannot be pointed at a repo, so a repo: op in this call would apply to some ops
and be silently ignored by this one. Drop the repo: op, or give the repo-scoped ops a call of
their own.

$ ./supertool 'repo:owner/name' 'gh-issue-create:@.max/new.toml'
repo: 'gh-issue-create' takes its repo target in the payload (repo = "OWNER/NAME"), not from a
repo: op — so there is one place the target comes from. Set it there and drop the repo: op.
```

Both fail before any op runs. A target that quietly applied to part of a call is the shape of the bug this fixed, so it is not the fix's behaviour either. Ops opt in via `"repo_target": true` in the preset manifest (`"payload"` for those routing it through their own payload), which is also what makes the refusal able to name *which* of the two problems you have.

### The error names the door, not just the wall

`ERROR: cwd is not a GitHub repo` was complete and honest while cwd was the only way to name a repo. With a second route it describes a wall that now has a door, so it names both — and when a target *was* given it is not used at all, because the cwd had no part in that lookup:

| Situation | Message |
|---|---|
| No target, cwd is not a GitHub clone | `cwd is not a GitHub repo and no repo target was given. cd into a GitHub-cloned repo, name one with a leading repo: op …, or run gh directly with --repo OWNER/REPO.` |
| Target given, `gh` cannot resolve it | `repo target 'owner/name' could not be resolved by gh. Check the spelling and your access: gh repo view owner/name` |
| Target given, the number is wrong | `PR #265 not found in owner/name. Check the number, or the repo target (gh repo view owner/name).` |

The third one used to read *not found in this repo … verify you're in the right repo*, which under a target sends the reader to inspect a working directory that took no part in the request.

### `gh-prs` declines its watch column under a target

Watch pollers write `supertool-watch-github-pr__{number}.pid` — keyed by PR number, with no repo in the key. Under a repo target that key is ambiguous: a live poller for `#12` of the repo you are standing in cannot be told apart from `#12` of the repo the board is about.

So the board declines rather than guesses. Rows print `?` instead of 👁 — and instead of blank, because blank on this board is the *claim* "no poller is watching this". The footer drops its ready-to-run `watch:github-pr:N` and says why:

```
0 PR(s) | watch state unknown for a repo target (keyed by number only) — watch from a clone of that repo
```

That clause is not decoration: it emits a command, and under a target that command would start polling *this* repo's `#N` while the board it came from was about another. An actionable suggestion that does the wrong thing is worse than no suggestion — and going quiet about it would read as "nothing needs watching". `ok`, a finding, and `skipped` are three different answers; see [Declining instead of guessing](../validators.md).

## Common workflows

**Review a PR before merging:**
```bash
./supertool 'gh-pr:42' 'gh-run:NUMBER'
```
`gh-pr` gives you the full dashboard (approval, diff stat, comments); `gh-run` confirms all checks passed. Replace `NUMBER` with the run ID shown in `gh-pr` output.

Read the two lines together. `Checks:` is CI; `Mergeable:` is GitHub's *merge conflict* state and nothing else, so it now says `Mergeable: yes (no merge conflicts)` and appends `— checks ⚠ NOT ALL GREEN, see Checks above` when the run is not unanimously green. `Mergeable: yes` beside a cancelled run was how a failed run got read as ready to merge.

### Zero check runs is four states, not one

A commit with no check runs used to print one sentence — `Checks: none reported — no check runs on this commit`, under `Mergeable: UNKNOWN — no checks reported`. That sentence covered two situations with opposite consequences: GitHub had not created the run yet (waiting is correct), or no run was ever coming (waiting is a deadlock). Both read as "not yet" to someone waiting to merge, and in one session both were read that way, by two readers ten minutes apart ([#585](https://github.com/Digital-Process-Tools/claude-supertool/issues/585)).

| State | When | `Checks:` | `Mergeable:` suffix |
|---|---|---|---|
| **not yet** | head commit younger than ~15min | `none yet — head commit 2m old, inside the ~15min window in which a first run has always appeared; a run is still expected` | `— no checks yet, a run is still expected` |
| **never** | head commit older than the window **and** PR merged/closed | `none, and none will be created — head commit 2d old and still zero runs, and the PR is MERGED, so no pull_request event will fire for this ref again. Waiting will not change this.` | `— no checks, and none will be created (PR is MERGED)` |
| **conflicting** | `mergeable` is exactly `CONFLICTING` | `none, and none will be created until the conflict is resolved — mergeable state is CONFLICTING, so GitHub cannot build refs/pull/N/merge for a pull_request run to execute against. Rebase — waiting will not change this.` | `— no checks, and none will be created (mergeable is CONFLICTING) — rebase` |
| **UNKNOWN** | anything else | `none reported — head commit 2h old and still zero runs, past the ~15min window …; the PR is OPEN, so an event could still fire and whether any workflow covers this ref is UNKNOWN. Check the PR's Checks tab.` | `— no checks reported, and whether any are coming is UNKNOWN` |

**The evidence is timestamps and PR state, never a parsed `on:` block.** Reading `.github/workflows/*` from the working tree infers a conclusion about the PR's head ref from files that need not be on it — a wrong "no run will ever be created" is the most expensive sentence this op can print. The empirical leg is stronger and cheaper: for an already-pushed head commit with zero runs, whatever `push` event applied has already fired and produced nothing, and on a merged or closed PR no `pull_request` event can fire again.

`git-status` renders these same four states from the same `_checks.absence()`, with a fifth line for the case only it has — a PR resolved by branch whose head SHA is not the local `HEAD`, so the checks describe a commit the reader is not looking at. Its evidence is local git rather than a GraphQL call, and it is documented where its cost is: [git.md → What `git-status`'s `Checks:` line is about](git.md#what-git-statuss-checks-line-is-about) ([#587](https://github.com/Digital-Process-Tools/claude-supertool/issues/587)).

**A `CONFLICTING` PR has zero runs permanently, and rendered as `UNKNOWN` that read as "wait" ([#594](https://github.com/Digital-Process-Tools/claude-supertool/issues/594)).** A `pull_request` workflow runs against `refs/pull/N/merge`, a ref GitHub builds by merging head into base — it cannot be built while the merge conflicts, so no run will ever execute and the Checks tab this op used to point at stays empty forever. Rebasing is the only thing that changes it, so the line says `Rebase`. The `Conflicts: YES — cannot merge` line carries the same suffix from the same call, so the two cannot disagree. `mergeable` was already in the `gh pr view` field list, so this leg costs nothing: **no path gained a network call.**

**Only an exact `CONFLICTING` match claims a conflict, and nothing claims the absence of one.** GitHub returns `mergeable: UNKNOWN` while it recomputes mergeability, and that state falls through to the three legs above **unchanged** — they are silent about conflicts, so an unsettled mergeability can reach an honest UNKNOWN-about-check-timing and never a confident claim in either direction. `git-status` additionally withholds `mergeable` unless the PR head is established equal to the local `HEAD`: "CONFLICTING, so rebase" is a statement about one commit, and making it about a commit the reader has moved past is [#587](https://github.com/Digital-Process-Tools/claude-supertool/issues/587)'s defect in new words.

**An open PR sitting well past the window is overdue, not decided.** It gets the age printed and the conclusion declined, per `docs/validators.md` ([Declining instead of guessing](../validators.md#declining-instead-of-guessing)) — an event can still fire for it. A failed timestamp lookup lands in the same `UNKNOWN` leg and never in "never".

**Cost:** the extra lookup (one GraphQL call for the head commit's date) is made **only** when the rollup is empty. A PR with check runs pays nothing, which is the overwhelmingly common case on this hot path. The window is `_checks.CHECK_CREATION_GRACE_SECS`, 15min — roughly 3x the worst first-run latency measured on this repo (99s, 165s, ~2min, 4.5min).

**Casing:** the full dashboard prints `Title Case:` labels throughout (`State:`, `Checks:`, `Mergeable:`, `Merge commit:`); `:status` prints `lowercase:` labels throughout (`state:`, `checks:`, `mergeable:`, `merge_commit:`) as part of its terser, ~250-byte machine-oriented format. Each mode is internally consistent but the two never match each other, so grep the casing for the mode you're reading, not both — `grep 'Checks:'` finds nothing in `:status` output, and that silence means "wrong case", not "no checks".

### A red tally names its legs

`checks: 14 total: 1 passed, 5 failed, 8 pending ⚠ NOT ALL GREEN` answers "how many". It does not answer "which" — and a Windows-only red and an ubuntu-only red call for opposite actions (read the failure vs. `gh run rerun --failed`). Any check whose state is not `SUCCESS` and not still moving now gets its own named line, directly under the `Checks:`/`checks:` line, in **both** the full dashboard and `:status` — the terse form is what gets read on every poll, so it is exactly where naming pays off most ([#619](https://github.com/Digital-Process-Tools/claude-supertool/issues/619)):

```
checks: 14 total: 1 passed, 5 failed, 8 pending ⚠ NOT ALL GREEN
  failed: pytest (ubuntu-latest, 3.9) (job #91015853871), pytest (ubuntu-latest, 3.10) (job #91015853935), pytest (ubuntu-latest, 3.11) (job #91015853882), pytest (ubuntu-latest, 3.12) (job #91015853868), pytest (macos-latest, 3.12) (job #91015853854)
```

Two buckets are deliberately left unnamed: **passed** (not the reader's problem) and **pending** (resolves itself — naming eight still-queued legs on every poll is exactly the noise the terse form exists to avoid). Everything else is named, including the states that used to get folded into a bare count and read as "nothing outstanding" — `CANCELLED`, `SKIPPED`, `NEUTRAL`, `TIMED_OUT`, `ACTION_REQUIRED` each get their own `label:` line, same defect class as [#445/#454](https://github.com/Digital-Process-Tools/claude-supertool/issues/454), applied to the "which" question instead of the "how many" one.

**A job id rides along, for free.** GitHub's `statusCheckRollup` already carries `detailsUrl` (`.../actions/runs/<run>/job/<job>`) on every call `gh-pr` makes — no extra request. Parsing the id out of it is what makes `gh-job:91015853871:fail` reachable with no `gh api .../actions/jobs` detour, which was the more expensive half of the original fallback.

**Bounded at 5 legs per group**, then `+N more` — this repo's disclosure vocabulary ([#605](https://github.com/Digital-Process-Tools/claude-supertool/issues/605)) — so a wide matrix cannot turn the answer to "which" into a second output-budget problem.

**Debug a failed Actions job:**
```bash
./supertool 'gh-run:12345'
# find the failed job ID, then:
./supertool 'gh-job:67890'
# if you need the full log:
./supertool 'gh-job:67890:raw:1:100'
# or just the tail, without knowing how long the log is:
./supertool 'gh-job:67890:raw:-40'
```

**Build a follow list from a repo's community:**
```bash
./supertool 'gh-find-followable:anthropics/claude-code|100'
# review the output, save to a file, then:
./supertool 'gh-batch-follow:.max/follow-list.txt'
```

### The linked issue is a declared closing reference, not the first `#N`

`gh-pr` and `git-status` both used `(?:closes|fixes|resolves)?\s*#(\d+)` — the keyword group optional, which reduces the pattern to "the first `#<digits>` anywhere in the body". The `Issue:` line then stated that number as the issue the PR addresses, with no hedge ([#591](https://github.com/Digital-Process-Tools/claude-supertool/issues/591)).

A PR body routinely cites issues it does **not** close: a precedent (`the same shape as #263`), a sibling filed separately, a related discussion. Whichever appeared first won. The well-written body — context before subject — is the one most likely to trip it, and `gh-pr` went on to *fetch* the wrong issue and print its title, state, labels and assignees under this PR's closing reference.

| Body | Rendered |
|---|---|
| `This fixes the same class of bug as #263 … Closes #591` | `Issue: #591` — #263 is neither printed nor fetched |
| `Closes #571 and closes #572` | `Issues: #571, #572` — plural label, every reference, deduped in order |
| `Background: see #454` | `Issue: none declared in the body — no closing keyword (Closes/Fixes/Resolves #N) bound to an issue number. A bare #N mention is not a closing reference to GitHub and is not reported as one here; a link made through the PR's Development panel is not in the body and is invisible to this line.` |
| `Closes octo/other#5` | `Issue: octo/other#5 — in another repository, not fetched` |
| ``Example: `Closes #572` `` | nothing — a code span is not a declaration, because GitHub does not read one either |

**GitHub's keyword set, verbatim.** `close`/`closes`/`closed`, `fix`/`fixes`/`fixed`, `resolve`/`resolves`/`resolved`, case-insensitive. That list is what actually decides whether merging the PR closes the issue, so a narrower one here would silently drop an issue this PR really does close — a divergence between what we print and what the merge does is its own trap. The reference shapes GitHub honours are all accepted: `#N`, `GH-N`, `owner/repo#N`, and the full `https://github.com/owner/repo/issues/N` URL.

**The keyword is bound to its own number.** Separator is horizontal whitespace plus an optional colon, so `Closes: #591`, `closes  #591`, `Closes#591` and `Fixed #591` all extract — and deliberately **not** `\s*`, which spans newlines: `This fixes` / blank line / `#263 is a precedent` would extract #263 from a sentence whose keyword never names a number, the same defect in a thinner disguise. `This fixes the bug filed as #263` claims nothing. Over-matching is not the safe direction either.

**Nothing declared is a printed sentence, not a skipped line.** A missing `Issue:` line is indistinguishable from a renderer that never looked — the three-state contract of [Zero check runs](#zero-check-runs-is-four-states-not-one), one line further up the same output.

**A cross-repo reference is never resolved locally.** `gh issue view 5` resolves 5 against the *current* repository, so fetching `octo/other#5` here would print a different issue's title under this PR's closing reference — this defect with more confidence attached. Those refs are printed as written.

**Code spans, fenced blocks and HTML comments are removed before matching, and that rule was measured rather than assumed.** [#600](https://github.com/Digital-Process-Tools/claude-supertool/pull/600)'s own body cites ``Closes #571 and closes #572`` inside a code span as an example of this table, and GitHub's `closingIssuesReferences` for that PR returned `{571, 591}` — 571 from a prose sentence elsewhere in the body, 572 from nowhere. One variable, one observation: GitHub skipped the span. With the rule in place the extractor returns `['#591', '#571']` for that body — exact agreement with GitHub on a real PR. Four-space indented blocks are deliberately not handled: distinguishing one from a nested list continuation needs a real block parser, and guessing wrong would delete prose and drop a genuine reference.

Both ops call `_checks.closing_issue_refs()` and `_checks.linked_issue_line()`, so the extraction and the wording cannot drift between them. The GitLab arm still uses its own `#(\d{4,})` heuristic with no keyword requirement — same class of defect, different closing vocabulary, filed separately.

### `gh-job:...:grep:` bounds its own output, and says when it did

Identical to `gl-job`'s — see
[gitlab.md](gitlab.md#grep-bounds-its-own-output-and-says-when-it-did) for the
incident and the reasoning. The knob here is `GH_JOB_GREP_MAX_BYTES` (default
65536). A capped view says so in its header *and* its footer, states an exact
`N of M matching lines shown`, and names **bytes** as what cut — never a match
limit, which this op does not have
([#622](https://github.com/Digital-Process-Tools/claude-supertool/issues/622)).

## Configuration

`gh-job` error pattern search is configurable via JSON:

```json
{
  "ops": {
    "gh-job": {
      "cmd": "python3 {path}github/job.py {args}",
      "lines": 120,
      "error_patterns": "ERROR,FAILED,Error:,Failed,fatal:,##[error]",
      "error_context": 10
    }
  }
}
```

Otherwise inherits from `gh auth status` — no project-specific tokens needed.

### Default repo for `gh-issue-create`

`gh-issue-create` needs a `repo` in every payload. To omit it, set a default:

```json
{
  "defaults": {
    "github_repo": "Digital-Process-Tools/claude-supertool"
  }
}
```

Resolution order (most specific wins): explicit `repo` in the payload → `defaults.github_repo` → the `origin` git remote when its host is `github.com`. In a GitHub checkout the remote covers it with zero config; the explicit default is for when `origin` points elsewhere.

**A missing `@FILE` is declined, not crashed on.** `gh-issue-create` invoked with no payload argument at all — `./supertool gh-issue-create` — used to hit `Path("").read_text()`, which resolves to the current directory, and leak a five-frame `IsADirectoryError` traceback ([#620](https://github.com/Digital-Process-Tools/claude-supertool/issues/620)). It now prints `ERROR: gh-issue-create needs a payload — gh-issue-create:@FILE (JSON or TOML with title/body).` and exits 1. An `@FILE` that names an actual directory reports `ERROR: payload path is a directory, not a file: PATH` instead of the same traceback, and a payload that fails to parse names the expected shape rather than only echoing the parser's own message.

**`body_file` gets the same treatment as the payload itself** ([#630](https://github.com/Digital-Process-Tools/claude-supertool/issues/630)). A missing, directory, or unreadable `body_file` used to leak a raw traceback — the payload load had a guard, the second read ten lines later did not. It now reports `ERROR: body_file not found: PATH`, `ERROR: body_file is a directory, not a file: PATH`, or `ERROR: permission denied reading body_file: PATH — ...`, naming the field so it's never confused with a payload-file error.

## Authoring notes

Preset JSON: `presets/github.json`. Helper scripts: `presets/github/` — one Python file per op. `gh-find-followable` and `gh-find-starable` are discovery ops: they produce a list for human review, not an immediate action. Always review the file before running `gh-batch-follow` or `gh-batch-star`.
