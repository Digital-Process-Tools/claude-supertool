# git

Git investigation and workflow ops. Replaces the 4-6 raw `git` calls you'd normally chain to get a usable picture of a repo's state — `git status`, `git log`, `git diff`, `git blame`, `git log -S`, `git log --follow`. Each op packs the next question's answer into the current call so the agent doesn't need a follow-up turn to decide what to do.

## Requires

`git` installed and a git repository. No auth, no tokens.

## Ops

| Op | Syntax | What it returns |
|----|--------|-----------------|
| `git-status` | `git-status[:full]` | Branch, tracking, ahead/behind, last 5 commits, staged/unstaged/untracked files, stashes, open MR/PR link, suggested next step. The `Issue:` line reports only issues a GitHub closing keyword binds to (`Issue: #591`, `Issues: #571, #572`, or a stated `none declared`) — never the first `#N` in the body ([#591](https://github.com/Digital-Process-Tools/claude-supertool/issues/591), see [What `git-status`'s `Issue:` line claims](#what-git-statuss-issue-line-claims)). The default view caps each list (20 staged/unstaged, 10 untracked/branches, 5 stashes) with a `... (N more)` marker — cheap overview. `:full` (alias `:porcelain`) **uncaps every list** for the full untruncated view, e.g. when you need to drive precise staging (excluding a few pre-existing untracked items from a large commit) and can't from a truncated list |
| `git-investigate` | `git-investigate:PATH` | File history: recent commits touching the file, uncommitted changes, blame hotspots (most-recently-changed lines) |
| `git-trail` | `git-trail:PATTERN:PATH` | Trace a symbol or string through history via pickaxe search — when it was added, modified, or removed, with contextual diff hunks. **Both of its caps state themselves** ([#635](https://github.com/Digital-Process-Tools/claude-supertool/issues/635)): the `## Timeline` list is bounded by `SUPERTOOL_MAX_COMMITS` (default 20) and the `## Details` section renders at most `SUPERTOOL_TRAIL_DETAIL_CAP` commits (default 10) because it costs one `git show` each. When either bites, the marker is in the **header as well as** the footer — `[CAPPED: 10 of 47 commits shown by count — raise …]` — and it names a *count* limit, which is what actually cut, rather than a size budget. A capped timeline makes the detail denominator read `10 of 20+`, never a total nobody measured. **When nothing is cut, nothing extra is printed**, so an unmarked result is a positive claim that the trail is whole |
| `git-blame` | `git-blame:PATH:LINE[:N]` | Blame for N lines (default 5) around a specific line number |
| `git-checkout` | `git-checkout:REF` | Switch to branch/tag/SHA — reports tracking state, dirty files, last commits after switch |
| `git-diverge` | `git-diverge:BRANCH[:BASE]` | Branch vs base: ahead/behind counts, commit list, changed files, +/− line totals |
| `git-diff` | `git-diff[:staged\|:branch[:BASE]\|:PATH][:full]` | Review-aware diff (working / `staged` / `branch` merge-base / `PATH`): files grouped by kind + shortstat, red-flag scan of **added** lines (debug code, conflict markers; reported `file:line`), forbidden-path guard, missing-test pairing, next-step hints. Generic defaults built in; project policy via config (below). Every mode stamps a `Repo: <toplevel>` header so a wrong-CWD invocation is obvious. In `:PATH` mode a path that is **missing** under the current repo warns `not found … — wrong CWD?` and exits 1, an **untracked** on-disk path warns `untracked (not in git)` — neither is silently reported as `No changes.` (which now means only a clean *tracked* file). Append a trailing **`:full`** in any mode (`git-diff:full`, `git-diff:PATH:full`, `git-diff:staged:full`, `git-diff:branch:BASE:full`) to print the raw `+/-` hunks under a `## Patch` section below the summary — for reading the actual change or writing an honest commit message, without dropping to raw `git diff` |
| `git-merge` | `git-merge:REF` | Merge REF — on conflict surfaces the UU file list, conflict markers, and ours/theirs SHAs |
| `git-conflicts` | `git-conflicts` | List all UU files + every conflict block + abort hint |
| `git-resolve` | `git-resolve:::SIDE:::PATH[,PATH...][:::BLOCKS]` | Pick `ours`/`theirs`/`both` for one file, a comma-separated list, or `all` — stages and prints the continue command. `both` is a union: it strips the conflict markers and keeps both sides (ours then theirs), like git's `merge=union` driver — use it when both branches added different non-overlapping lines. Optional **`BLOCKS`** selector (e.g. `1,3`) resolves only those 1-indexed conflict blocks of a **single** file, numbered exactly as `git-conflicts` lists them; one side per call (mixed sides → run twice). A **partial** resolve leaves the other blocks' markers in place by design, so the file stays conflicted and **unstaged** — the receipt reads `N of M block(s) resolved, file still conflicted`; only when the selector covers every block does the file go clean and get staged. **Self-verifies before staging:** a leftover `<<<<<<<` / `>>>>>>>` is a hard fail (file left unstaged), and each resolved file's receipt carries a warn-only validator digest (`markers: clean \| validate: ok`) |
| `git-commit` | `git-commit:::MESSAGE[:::PATHS...]` | Stage PATHs (or all staged if omitted) and commit with MESSAGE — surfaces hook errors, shows HEAD before/after. Use `MESSAGE=--no-edit` to reuse MERGE_MSG/CHERRY_PICK_HEAD during an in-progress merge or cherry-pick. **Multi-line body:** use the `@file` route — `git-commit:@-` (stdin) or `git-commit:@msg.toml` — with a `message` field (subject + blank line + body) and an optional `paths` list, instead of dropping to raw `git commit -F` (which skips the trailer below). Auto-appends a `Co-Authored-By:` trailer when the message lacks one (default `Max <noreply>`), on both the colon-CLI and `@file` routes — configurable via `.supertool.json` (`ops.git-commit.coauthor`) or the `SUPERTOOL_COAUTHOR` env var; disable with an empty value or `none`/`off`/`false` |
| `git-push` | `git-push[:force-with-lease][:no-verify][:watch]` | Push the current branch (sets upstream on first push) — remote SHA before/after with commits pushed, ahead/behind vs upstream, and the open MR/PR + pipeline status. For **updating** an already-open MR; use the `mr` op for push+create. **Non-fast-forward** is handled in-op: it fetches, surfaces the **incoming remote commits** (SHA, author, subject) so you can see whose work you'd be rebasing over, then rebases your work onto the remote and re-pushes; on conflict it leaves the rebase **paused**, warns to check the incoming authors before forcing, and points you at `git-conflicts` + the keep-both/cancel/force paths — never auto-forced, never silently rewritten. A **pre-push hook that amends HEAD and pushes** the fixed commit itself (exiting non-zero) is reported as `PUSHED`, not `REJECTED`, since the live remote ref already matches HEAD. A **push that outlasts its budget** (300s, under the op's 420s cap so this script owns the timeout) gets the same treatment rather than a bare timeout failure: `ls-remote` is asked what landed, a remote ref matching HEAD reports `pushed ✓`, and only a remote that genuinely did not move reports `PUSH TIMED OUT ✗` — unverified rather than rejected, with an explicit *fetch before retrying, never force-push on a timeout alone*. The **post-push receipt** carries the next-decision signals (all on calls already made): MR **mergeability** (warns if it now `cannot_be_merged` with target), **stale base** (`N behind origin/<target>`), **uncommitted leftovers** (changes not in this push), the **pipeline id + url**, and a ready `watch:gitlab-mr:<iid>` command. `:force-with-lease` also reports **what it discarded** (author + subject of overwritten remote commits). **Every run ends on a one-line `[result]` verdict** ([#623](https://github.com/Digital-Process-Tools/claude-supertool/issues/623)) — `PUSHED  <branch> -> <remote>/<ref> @ <sha>  (verified)`, or `NOT PUSHED - …` naming which of *already up to date* / *REJECTED* / *REBASE PAUSED* / *UNVERIFIED (timed out)* / *no push attempted (not a repo, detached HEAD)* happened. It is the **last** line, because a verdict that is merely present is one an untracked-file list or a 40-line hook dump scrolls off the end of `| tail -6`; a push that did **not** happen must never render like one that did. The sha in that line is read back off the real remote with `ls-remote`, not just the local remote-tracking ref, so you do not have to `git fetch && git log FETCH_HEAD` to trust it — and when the remote does not answer it says `unverified` and falls back to the tracking sha, labelled, rather than printing a sha it never read. **Uncommitted leftovers are a count, not a listing** — `⚠ N change(s) NOT in this push (uncommitted) — list them: ./supertool 'git-status:full'`; on a working tree full of generated junk the listing was the entire tail of the output. Flags: `:force-with-lease` (safe force — overwrite only if the remote hasn't moved; skips the auto-rebase, and lists discarded commits), `:no-verify` (skip the local pre-push hook, e.g. when a local formatter diverges from CI), `:watch` (spawn a background pipeline poller instead of just recommending the command — it falls back to `python3 supertool.py` where the gitignored `./supertool` wrapper is absent, e.g. inside a git worktree, and **names the reason** if it still cannot start rather than degrading to the manual hint ([#642](https://github.com/Digital-Process-Tools/claude-supertool/issues/642))). An **unrecognised flag is refused before anything is pushed** and the op exits `2` naming it and listing what is accepted ([#647](https://github.com/Digital-Process-Tools/claude-supertool/issues/647)) — `git-push:no-verifyy` used to run an ordinary *verified* push while the caller believed the hook had been skipped. A **fetch or rebase on the non-fast-forward recovery path that outlasts its budget** ends on a verdict naming the **worktree state**, not a traceback ([#640](https://github.com/Digital-Process-Tools/claude-supertool/issues/640)): `REBASE IN PROGRESS` with both exits (`git rebase --continue` / `git rebase --abort`), `no rebase started, working tree unchanged`, or an explicit `rebase state UNKNOWN, run git status` when the tool cannot tell — three states, never a guess. The **stale-base check follows the branch's real upstream remote** rather than a hardcoded `origin`, so it still fires on a fork/upstream layout, and prints `⚠ stale-base check skipped` when the target ref does not resolve locally — silence from it means only *the check ran and the base is fresh*. The **force-push discard check has three states too** ([#655](https://github.com/Digital-Process-Tools/claude-supertool/issues/655)): `Force discarded N remote commit(s)` with the list when it ran and found them — and `FORCE-DISCARDED N` on the `[result]` line, so it survives `| tail -3` — **nothing at all** when it ran and found none, and `⚠ DISCARD CHECK DID NOT RUN — <why>` naming the reason plus the command that settles it (`git log <old> --not HEAD`, or `git reflog show <remote>/<branch>`) when it could not. Both quiet states used to be the same empty list as the clean one, so a failed `git log` rendered exactly like a force-push that destroyed nothing — on the one operation here that destroys work irrecoverably, about commits that are usually somebody else's, and `--force-with-lease` does not answer it (a current lease still discards commits you never saw). The pre-push SHA falls back to the one **git itself reports** on the `--porcelain` channel (`+ <old>...<new> (forced update)`), which closes the reachable hole where `@{upstream}` was unset while the remote-tracking ref was current: the lease passed, the push destroyed a colleague's commit, and the check was skipped silently. It never blocks, aborts or re-pushes — the force is your decision; it only refuses to call an unchecked push a clean one |

## Common workflows

**Assess state before starting work:**
```bash
./supertool 'git-status' 'git-diverge:my-feature:master'
```
One call gives you branch health + exactly what differs from base — no follow-up needed.

**Investigate why a file changed:**
```bash
./supertool 'git-investigate:src/app/Auth.py' 'git-trail:verify_token:src/app/Auth.py'
```
`git-investigate` shows recent commits and blame hotspots; `git-trail` pinpoints when `verify_token` was added or renamed.

**Resolve a merge conflict and commit:**
```bash
./supertool 'git-conflicts'
# review, then:
./supertool 'git-resolve:::ours:::src/app/Config.py'
# or keep both sides when each branch added different lines:
./supertool 'git-resolve:::both:::src/app/Config.py'
./supertool 'git-commit:::Merge master into feature/auth'
```
Every resolve **self-verifies before it stages**: it scans the resolved file for
leftover conflict markers (`<<<<<<<` / `>>>>>>>`) and then runs the file-type
validator (xmllint / phplint / …) so the receipt tells you the resolution is
actually clean — the routine you'd otherwise run by hand:
```text
# git-resolve: ours (1 file(s))
  ✓ src/app/Config.py
      markers: clean | validate: ok
Resolved: 1 | Failed: 0 | Remaining: 0
Next: ./supertool 'git-commit:::Merge resolved' (or git merge --continue)
```
A marker left behind is a **hard fail** — the file is *not* staged, so a broken
merge can never reach a commit:
```text
  ✗ src/app/Config.py: conflict markers remain at line(s) 63 — not staged
```
The validate line is **advisory** (warn-only): `validate: ⚠ phplint 2 err` flags
syntax to look at but never blocks the resolve — an "invalid" file is often just
a merge that needs the next hunk resolved, not a corrupt write. It is scoped to
**parser/compiler** validators (phplint, xmllint, jsonlint, …), never semantic or
style ones (lsp-diag, pyright, prettier), since only a parse break is something a
side-pick can introduce — and it is omitted entirely when no syntax validator is
configured for that file type (you'll just see `markers: clean`).

**Resolve specific conflict blocks (per-hunk side selection):**
```bash
./supertool 'git-conflicts'                          # blocks are numbered per file
# keep ours for blocks 1 and 3 only — block 2 stays conflicted:
./supertool 'git-resolve:::ours:::src/app/Config.py:::1,3'
```
One side per call (mixed sides → run twice). A partial resolve never stages — it
keeps the unselected blocks' markers and reports what's left:
```text
# git-resolve: ours block(s) 1, 3 in src/app/Config.py
  ~ src/app/Config.py: 2 of 3 block(s) resolved, file still conflicted
Resolved blocks: 2 | Remaining blocks: 1 | Not staged (still conflicted).
Next: resolve the remaining block(s), then ./supertool 'git-resolve:::SIDE:::PATH' (whole file) or git add once clean.
```
When the selector covers every block the file goes clean and is staged like a
whole-file resolve.

**Update an MR that already exists:**
```bash
./supertool 'git-commit:::Fix the thing:::src/app/Thing.py' 'git-push'
```
`git-commit` shows HEAD before/after; `git-push` updates the remote and reports the open MR + the pipeline the push just triggered — no raw `git push` fallback.

**Just tell me whether it landed:**
```bash
./supertool 'git-push' | tail -1
```
```
[result] PUSHED  max/a11y-aria-prohibited-attr -> origin/max/a11y-aria-prohibited-attr @ f1627b7  (verified, 2 commit(s))
```
`verified` means the sha was read back from the remote with `ls-remote` after the push, so this replaces the `git fetch` + `git log FETCH_HEAD` round-trip. A run that did not push is equally unambiguous at `tail -1`:
```
[result] NOT PUSHED - already up to date  feat -> origin/feat @ f1627b7  (verified)
[result] NOT PUSHED - REJECTED  feat -> origin/feat - error: failed to push some refs to 'origin'
[result] NOT PUSHED - REBASE PAUSED (conflict in 2 file(s))  feat -> origin/feat - resolve then `git rebase --continue`, or `git rebase --abort`
[result] NOT PUSHED - UNVERIFIED  feat -> origin/feat - push timed out and the remote does not match local HEAD (remote 07a15e1, HEAD b371919)
[result] NOT PUSHED - no push attempted (detached HEAD - checkout a branch first)
```

**Push when the remote has moved ahead (non-fast-forward):**
```bash
./supertool 'git-push'
```
`git-push` rebases your work onto the remote and re-pushes in the same call — no manual `pull --rebase` round-trip. If the rebase conflicts, it stops with the rebase **paused** and the conflicting files listed, then points you at `git-conflicts`:
```bash
./supertool 'git-conflicts'                       # inspect the blocks
./supertool 'git-resolve:::ours:::src/app/X.py'   # decide
# then continue the rebase and push:
git rebase --continue && ./supertool 'git-push'
```
To **cancel** and get back to exactly where you were before the push (your commits intact, nothing pushed), `git rebase --abort`. To overwrite the remote intentionally (your history wins), `git rebase --abort` then `./supertool 'git-push:force-with-lease'`. To skip a local pre-push hook that diverges from CI, `./supertool 'git-push:no-verify'`.

**Only git decides that the remote moved ahead.** The auto-rebase is the one path in this op that rewrites local history, so it fires on git's own machine-readable answer and nothing else: the push runs `--porcelain`, and the per-ref status line for your branch on stdout — `!<TAB>refs/heads/x:refs/heads/x<TAB>[rejected] (non-fast-forward)` — is the only input to the decision. Text printed by a **pre-push hook** cannot reach it, even when the hook prints the exact words git uses (`fetch first`, `non-fast-forward`, `tip of your current branch is behind`); before [#641](https://github.com/Digital-Process-Tools/claude-supertool/issues/641) it could, and a hook saying "fetch first" in its own advice was enough to make the op fetch and rebase your branch.

The practical consequence: when a **local hook blocks the push**, git never contacts the remote and emits no ref status at all. That is not a divergence, and the op will not guess it into one — it stops, prints the hook's own output, and says so:

```
Hint: git reported no ref status for origin/feat — the push was stopped before it
reached the remote (local pre-push hook, or transport). Not a divergence: a rebase
would not help. The output below is what stopped it; `git-push:no-verify` skips a
local hook.
[result] NOT PUSHED - REJECTED  feat -> origin/feat - <the hook's first error line>
```

`[remote rejected]` (protected branch, pre-receive hook) is likewise never treated as a divergence — it keeps its own "a rebase will not help" hint.

## Configuration

Most ops need no project config. `git-investigate` takes two env vars (below); `git-diff` takes optional review policy (see **git-diff policy** below).

| Variable | Default | Effect |
|----------|---------|--------|
| `SUPERTOOL_COMMITS` | `10` | Number of recent commits to show |
| `SUPERTOOL_BLAME_RECENT` | `5` | Number of blame hotspot lines to surface |

Set via the op's JSON config if you want project-wide defaults:
```json
{
  "ops": {
    "git-investigate": { "cmd": "python3 {path}git/investigate.py {arg}", "commits": 20 }
  }
}
```

`git-status` and `git-push` try `glab` then `gh` to surface the open MR/PR — skip gracefully if neither is installed.

### What `git-status`'s `Checks:` line is about

The four states of zero check runs are defined once, in [github.md → Zero check runs is four states, not one](github.md#zero-check-runs-is-four-states-not-one) — `none yet` inside the creation window, `none, and none will be created` once the head commit is past it on a merged/closed PR, `none … until the conflict is resolved — Rebase` when `mergeable` is `CONFLICTING`, a stated `UNKNOWN` otherwise. `git-status` renders the same four from the same `_checks.absence()`, so the wording never drifts between the two ops ([#587](https://github.com/Digital-Process-Tools/claude-supertool/issues/587), [#594](https://github.com/Digital-Process-Tools/claude-supertool/issues/594)).

The conflict leg is passed `mergeable` **only** when the PR's head SHA is established equal to the local `HEAD`, and `None` otherwise — a rebase instruction is about a specific commit, and stating it about one the reader is not standing on is the same defect as a green tally for a commit you have moved past. Withheld, it falls through to the other three legs unchanged. `mergeable` rides the `gh pr view` call already being made, so this arm still pays no network call on any path.

Two things differ here, both because `git-status` resolves the PR **by branch** while standing in a working tree.

**The age is read from the local object store, not the network.** `gh-pr` holds only a PR number and pays one GraphQL call for the head commit's date. `git-status` is in the repo, and the PR's head commit is almost always already here — you pushed it — so the date comes from `git log -1 --format=%ct <headRefOid>`. `headRefOid` rides along in the `gh pr view` call already being made. **Total added cost when nothing is wrong: one local `git rev-parse HEAD`. No network call on any path**, which matters because `git-status` is the most frequently run op in the tool *and* the zero-runs leg is its common case — running it right after a push is the whole point.

**A fourth line appears when the checks are not about your `HEAD`.** Your local `HEAD` can be ahead of, behind, or unrelated to the PR's head SHA, and then every check run fetched for the PR describes a commit you are not looking at — `Checks: 12 total: 12 passed` reads as "your work is green" while your two unpushed commits are untested. So whenever the two SHAs are not established equal:

```
Checks commit: PR head 1a2b3c4 — NOT your local HEAD 9f8e7d6. The Checks line
above is about the PR's head commit, not the commit you are standing on.
`gh-pr:587` for that commit.
```

Printed for a **passing** tally too, not only for an absent one — a true statement about the wrong commit is the failure mode, and the tally is where it is most convincing.

**Every unestablished lookup lands in `UNKNOWN`, by construction.** A head SHA this clone has never fetched cannot be dated, so `absence()` is handed `None` and cannot reach "none will be created" — the one skew that would make this worse than the sentence it replaced. A `headRefOid` that is not a full 40-hex object name is refused outright rather than resolved: `HEAD` and `master` are valid revision arguments that resolve *locally* to the wrong commit, and dating one of those and captioning it as the PR head's age is the same defect one layer along. Silence is reserved for the two SHAs being established **equal**; an unknown relation says so, because printing nothing reads as "same commit".

### What `git-status`'s `Issue:` line claims

Only a GitHub closing keyword bound to its own number. `Issue: #591` for one reference, `Issues: #571, #572` for several, and a stated `Issue: none declared — …` when the body names no closing keyword at all — never the first `#N` it can find, which is what the shared pattern used to do ([#591](https://github.com/Digital-Process-Tools/claude-supertool/issues/591)). Defined once, with the keyword set, the accepted reference shapes and the cross-repo rule, in [github.md → The linked issue is a declared closing reference](github.md#the-linked-issue-is-a-declared-closing-reference-not-the-first-n). Both ops render it from the same `_checks.closing_issue_refs()`/`_checks.linked_issue_line()` pair, so the wording cannot drift.

The GitLab arm of this op is unchanged and still uses `#(\d{4,})` on the MR description — any four-plus-digit number, no keyword required. Same class of defect on GitLab's numbering, filed separately: it needs GitLab's own closing vocabulary rather than a GitHub-shaped helper.

### `Pipeline:` on the GitLab arm

`Pipeline: none` was the GitLab spelling of the same ambiguity, and it read as the expensive leg for free — "there is no CI on this ref" — when it equally covers a head that was just pushed. It now declines: `Pipeline: none reported — whether one is still coming is UNKNOWN …`.

There is deliberately **no grace window** on this arm. The ~15min on the GitHub side is measured GitHub creation latency; inventing a GitLab equivalent with no measurement behind it would be guessing in the shape of evidence. So the GitLab leg only ever declines, and a measured window can be added later. `gl-mr` and the `gl-mrs` board still print a bare `none` — filed separately, since a table column cannot hold a sentence and a triage list is not a merge decision.

### git-diff policy

`git-diff` ships generic red-flag defaults (debug-code and conflict markers) and reads optional **project policy** from its op config, surfaced to the script as `SUPERTOOL_*` env vars — the same mechanism `gl-job` uses for `job_patterns`. All four keys are optional.

| Key | Shape | Effect |
|-----|-------|--------|
| `forbidden_paths` | `[{pattern, reason}]` | Warn when a changed file's path matches `pattern` (regex); `reason` is printed verbatim. |
| `test_pairing` | `[{src, test}]` | For each **added** source file matching `src` (regex with named groups), warn if the derived `test` path exists neither in the diff nor on disk. `test` is a template — `{name}` placeholders are filled from `src`'s named captures. |
| `hints` | `[{added, message}]` | Print `message` once if any **added** path matches `added` (regex) — for follow-up reminders. |
| `red_flags_extra` | `[{pattern, ext?, label}]` | Extra added-line red flags on top of the defaults. `pattern` (regex) is tested per added line; optional `ext` (e.g. `.js`) scopes it to one extension; `label` names the hit. |

```json
{
  "ops": {
    "git-diff": {
      "forbidden_paths": [
        { "pattern": "/Generated/", "reason": "generated — edit the source class, not this" }
      ],
      "test_pairing": [
        { "src": "src2/(?P<rest>.+)\\.class\\.php$", "test": "tests/unit/{rest}Test.php" }
      ],
      "hints": [
        { "added": "src2/.+\\.class\\.php$", "message": "new class — regenerate XSD + autoload cache" }
      ],
      "red_flags_extra": [
        { "pattern": "^\\s*var\\b", "ext": ".js", "label": "var (use const/let)" }
      ]
    }
  }
}
```

The `test_pairing` capture→template pairing is the one non-obvious bit: `src2/(?P<rest>.+)\.class\.php$` captures `rest` (e.g. `SiFoo/Bar`), and `tests/unit/{rest}Test.php` expands to `tests/unit/SiFoo/BarTest.php`. Pairing fires only for **added** files classified as source — renames, edits, and non-source files are never flagged.

**Dogfood:** this repo's own `.githooks/pre-commit` runs `git-diff:staged` as an advisory review (never blocks). Wire it into any project the same way.

## Authoring notes

Preset JSON: `presets/git.json`. Helper scripts: `presets/git/` — one Python file per op (`status.py`, `investigate.py`, `trail.py`, etc.). The `{path}` placeholder in `cmd` resolves to `presets/git/` at runtime. Helpers shared across scripts (`_git`, `_first_error_line`, the glab→gh `query_open_mr` lookup) live in `presets/git/_git_common.py`; each script adds its own dir to `sys.path` then imports from it — named `_git_common` (not `_common`) to avoid colliding with `presets/claude-log/_common.py` when both load in the same test process.
