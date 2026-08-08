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
| `git-checkout` | `git-checkout:REF` | Switch to branch/tag/SHA — reports tracking state, dirty files, last commits after switch. Its three recoveries for a ref this clone has not got — `fetch --all --prune`, `checkout -b --track <remote>/<ref>`, and an explicit `fetch origin <ref>` + `checkout -B <ref> FETCH_HEAD` for a narrowed refspec — all rewrite local state, so **they turn on `git rev-parse`'s exit code, not on git's error message** ([#649](https://github.com/Digital-Process-Tools/claude-supertool/issues/649)). That message is translated: under `LANGUAGE=fr` it reads `le spécificateur de chemin … ne correspond à aucun fichier connu`, and while the recoveries keyed on the English `pathspec` / `did not match any` they silently did not fire for anyone whose git speaks another language. Same channel discipline as `git-push`'s non-fast-forward decision. The remaining hints (`uncommitted changes block checkout`, `checked out in another worktree`) still read git's prose and so are still English-only; they choose wording, never an action. **`REF` must name a ref, and an argument that names a path is refused** ([#756](https://github.com/Digital-Process-Tools/claude-supertool/issues/756)). `git checkout <arg>` is two operations sharing one name: given a ref it switches branches, given a pathspec it restores those paths from the index — discarding uncommitted work with no reflog entry, no stash and no object written, so there is nothing anywhere to recover it from. Passing `git-checkout:work.txt` used to do exactly that and then report `Working tree: clean`, which was true only because the op had just made it so. Now an argument that git resolves as a path but not as a commit is refused before anything runs, naming what was passed and pointing at `git checkout -- <path>` for anyone who genuinely wanted the restore; and **every switch is issued as `git checkout <ref> --`**, so the pathspec reading cannot be selected even for an argument the check did not anticipate. There is deliberately **no pathspec-restore op**: discarding uncommitted work is not a mode of a branch-switch command. An argument that is *both* — a `docs` branch beside a `docs/` directory — resolves to the **ref**, always, and says so rather than leaving it to git's DWIM. |
| `git-diverge` | `git-diverge:BRANCH[:BASE]` | Branch vs base: ahead/behind counts, commit list, changed files, +/− line totals |
| `git-diff` | `git-diff[:staged\|:branch[:BASE]\|:PATH][:full]` | Review-aware diff (working / `staged` / `branch` merge-base / `PATH`): files grouped by kind + shortstat, red-flag scan of **added** lines (debug code, conflict markers; reported `file:line`), forbidden-path guard, missing-test pairing, next-step hints. Generic defaults built in; project policy via config (below). Every mode stamps a `Repo: <toplevel>` header so a wrong-CWD invocation is obvious. In `:PATH` mode a path that is **missing** under the current repo warns `not found … — wrong CWD?` and exits 1, an **untracked** on-disk path warns `untracked (not in git)` — neither is silently reported as `No changes.` (which now means only a clean *tracked* file). Append a trailing **`:full`** in any mode (`git-diff:full`, `git-diff:PATH:full`, `git-diff:staged:full`, `git-diff:branch:BASE:full`) to print the raw `+/-` hunks under a `## Patch` section below the summary — for reading the actual change or writing an honest commit message, without dropping to raw `git diff` |
| `git-merge` | `git-merge:REF` | Merge REF — on conflict surfaces the UU file list, conflict markers, and ours/theirs SHAs |
| `git-conflicts` | `git-conflicts` | List all UU files + every conflict block + abort hint |
| `git-resolve` | `git-resolve:::SIDE:::PATH[,PATH...][:::BLOCKS]` | Pick `ours`/`theirs`/`both` for one file, a comma-separated list, or `all` — stages and prints the continue command. `both` is a union: it strips the conflict markers and keeps both sides (ours then theirs), like git's `merge=union` driver — use it when both branches added different non-overlapping lines. Optional **`BLOCKS`** selector (e.g. `1,3`) resolves only those 1-indexed conflict blocks of a **single** file, numbered exactly as `git-conflicts` lists them; one side per call (mixed sides → run twice). A **partial** resolve leaves the other blocks' markers in place by design, so the file stays conflicted and **unstaged** — the receipt reads `N of M block(s) resolved, file still conflicted`; only when the selector covers every block does the file go clean and get staged. **Self-verifies before staging:** a leftover `<<<<<<<` / `>>>>>>>` is a hard fail (file left unstaged), and each resolved file's receipt carries a warn-only validator digest (`markers: clean \| validate: ok`). **`both` is refused on source files** ([#744](https://github.com/Digital-Process-Tools/claude-supertool/issues/744)) — a union of two versions of code concatenates them, and the result *parses*, so neither the marker gate nor the validator digest can see that the block now runs twice. Refusal is **per file**: `git-resolve:::both:::all` over one `CHANGELOG.md` plus four `.py` resolves the changelog and holds back the four (`⊘ path: source file — 'both' concatenates …`, tally `Resolved: 1 \| Refused: 4`), leaving their markers in place so git itself blocks `rebase --continue`. Two ways through: append **`force`** (`git-resolve:::both:::PATH:::force`) to union anyway — the tally then discloses `Resolved: N (M source file(s) unioned — 'both' concatenates; verify manually)`, because the `validate: ok` beside it is true and useless — or declare the path `merge=union` in `.gitattributes`, which the op honours without a flag. The extension list (`.py .js .ts .php .rb .go .rs .java .sh .sql`, ~32) is a heuristic: it misses an extensionless script and over-fires on a `.sql` file of pure INSERT rows. **`both` is also refused on Markdown when the union would duplicate a heading** ([#839](https://github.com/Digital-Process-Tools/claude-supertool/issues/839)) — the *same* heading line on *both* sides of one hunk, which a line-level union emits twice, reparenting every line between the two copies under the first. On a Keep a Changelog file that turns unreleased entries into shipped ones while the receipt says `markers: clean`. The refusal names the heading it saw; `force` unions anyway and the tally discloses it (`Resolved: N (M file(s) with duplicated heading(s) — verify section structure)`); `merge=union` does **not** bypass this one, because the attribute answers *"union this file"*, not *"this union came out sound"*. The ordinary changelog conflict — two bullets under a shared `### Fixed`, heading outside the hunk — is untouched |
| `git-worktrees` | `git-worktrees[:PATH][:nopr]` | **Is an agent working in this worktree?** Every worktree (or one `PATH`) with branch, path, merged-into-base, and an occupancy verdict in **three states** — `occupied`, `idle`, `cannot tell` — each naming the evidence it was built from. `cannot tell` is the honest majority answer and is **not** `idle`; see [Occupancy has three states](#occupancy-has-three-states-and-idle-is-the-one-that-must-be-earned). Inspection only: nothing is removed, pruned, unlocked or written |
| `git-commit` | `git-commit:::MESSAGE[:::PATHS...]` | Stage PATHs (or all staged if omitted) and commit with MESSAGE — surfaces hook errors, shows HEAD before/after. Use `MESSAGE=--no-edit` to reuse MERGE_MSG/CHERRY_PICK_HEAD during an in-progress merge or cherry-pick. **Multi-line body:** use the `@file` route — `git-commit:@-` (stdin) or `git-commit:@msg.toml` — with a `message` field (subject + blank line + body) and an optional `paths` list, instead of dropping to raw `git commit -F` (which skips the trailer below). Auto-appends a `Co-Authored-By:` trailer when the message lacks one (default `Max <noreply>`), on both the colon-CLI and `@file` routes — configurable via `.supertool.json` (`ops.git-commit.coauthor`) or the `SUPERTOOL_COAUTHOR` env var; disable with an empty value or `none`/`off`/`false` |
| `git-push` | `git-push[:force-with-lease][:no-verify][:watch][:set-upstream\\|:to-upstream]` | Push the current branch (sets upstream on first push) — remote SHA before/after with commits pushed, ahead/behind vs upstream, and the open MR/PR + pipeline status. For **updating** an already-open MR; use the `mr` op for push+create. **Non-fast-forward** is handled in-op: it fetches, surfaces the **incoming remote commits** (SHA, author, subject) so you can see whose work you'd be rebasing over, then rebases your work onto the remote and re-pushes; on conflict it leaves the rebase **paused**, warns to check the incoming authors before forcing, and points you at `git-conflicts` + the keep-both/cancel/force paths — never auto-forced, never silently rewritten. A **pre-push hook that amends HEAD and pushes** the fixed commit itself (exiting non-zero) is reported as `PUSHED`, not `REJECTED`, since the live remote ref already matches HEAD. A **push that outlasts its budget** (300s, under the op's 420s cap so this script owns the timeout) gets the same treatment rather than a bare timeout failure: `ls-remote` is asked what landed, a remote ref matching HEAD reports `pushed ✓`, and only a remote that genuinely did not move reports `PUSH TIMED OUT ✗` — unverified rather than rejected, with an explicit *fetch before retrying, never force-push on a timeout alone*. The **post-push receipt** carries the next-decision signals (all on calls already made): MR **mergeability** (warns if it now `cannot_be_merged` with target), **stale base** (`N behind origin/<target>`), **uncommitted leftovers** (changes not in this push), the **pipeline id + url**, and a ready `watch:gitlab-mr:<iid>` command. `:force-with-lease` also reports **what it discarded** (author + subject of overwritten remote commits). **Every run ends on a one-line `[result]` verdict** ([#623](https://github.com/Digital-Process-Tools/claude-supertool/issues/623)) — `PUSHED  <branch> -> <remote>/<ref> @ <sha>  (verified)`, or `NOT PUSHED - …` naming which of *already up to date* / *REJECTED* / *REBASE PAUSED* / *UNVERIFIED (timed out)* / *no push attempted (not a repo, detached HEAD)* happened. It is the **last** line, because a verdict that is merely present is one an untracked-file list or a 40-line hook dump scrolls off the end of `| tail -6`; a push that did **not** happen must never render like one that did. The sha in that line is read back off the real remote with `ls-remote`, not just the local remote-tracking ref, so you do not have to `git fetch && git log FETCH_HEAD` to trust it — and when the remote does not answer it says `unverified` and falls back to the tracking sha, labelled, rather than printing a sha it never read. **Uncommitted leftovers are a count, not a listing** — `⚠ N change(s) NOT in this push (uncommitted) — list them: ./supertool 'git-status:full'`; on a working tree full of generated junk the listing was the entire tail of the output. Flags: `:force-with-lease` (safe force — overwrite only if the remote hasn't moved; skips the auto-rebase, and lists discarded commits), `:no-verify` (skip the local pre-push hook, e.g. when a local formatter diverges from CI), `:watch` (spawn a background pipeline poller instead of just recommending the command — it falls back to `python3 supertool.py` where the gitignored `./supertool` wrapper is absent, e.g. inside a git worktree, and **names the reason** if it still cannot start rather than degrading to the manual hint ([#642](https://github.com/Digital-Process-Tools/claude-supertool/issues/642))). An **unrecognised flag is refused before anything is pushed** and the op exits `2` naming it and listing what is accepted ([#647](https://github.com/Digital-Process-Tools/claude-supertool/issues/647)) — `git-push:no-verifyy` used to run an ordinary *verified* push while the caller believed the hook had been skipped. A **fetch or rebase on the non-fast-forward recovery path that outlasts its budget** ends on a verdict naming the **worktree state**, not a traceback ([#640](https://github.com/Digital-Process-Tools/claude-supertool/issues/640)): `REBASE IN PROGRESS` with both exits (`git rebase --continue` / `git rebase --abort`), `no rebase started, working tree unchanged`, or an explicit `rebase state UNKNOWN, run git status` when the tool cannot tell — three states, never a guess. The **stale-base check follows the branch's real upstream remote** rather than a hardcoded `origin`, so it still fires on a fork/upstream layout, and prints `⚠ stale-base check skipped` when the target ref does not resolve locally — silence from it means only *the check ran and the base is fresh*. The **force-push discard check has three states too** ([#655](https://github.com/Digital-Process-Tools/claude-supertool/issues/655)): `Force discarded N remote commit(s)` with the list when it ran and found them — and `FORCE-DISCARDED N` on the `[result]` line, so it survives `| tail -3` — **nothing at all** when it ran and found none, and `⚠ DISCARD CHECK DID NOT RUN — <why>` naming the reason plus the command that settles it (`git log <old> --not HEAD`, or `git reflog show <remote>/<branch>`) when it could not. Both quiet states used to be the same empty list as the clean one, so a failed `git log` rendered exactly like a force-push that destroyed nothing — on the one operation here that destroys work irrecoverably, about commits that are usually somebody else's, and `--force-with-lease` does not answer it (a current lease still discards commits you never saw). The pre-push SHA falls back to the one **git itself reports** on the `--porcelain` channel (`+ <old>...<new> (forced update)`), which closes the reachable hole where `@{upstream}` was unset while the remote-tracking ref was current: the lease passed, the push destroyed a colleague's commit, and the check was skipped silently. It never blocks, aborts or re-pushes — the force is your decision; it only refuses to call an unchecked push a clean one. **`(branch created)` is git's claim, not an inference** ([#661](https://github.com/Digital-Process-Tools/claude-supertool/issues/661)): it used to be printed whenever no pre-push SHA had been recorded, and that SHA is missing whenever `@{upstream}` does not resolve — a fact about local config that says nothing about the remote. Since `--force-with-lease` leases against the remote-tracking *ref*, unsetting only `branch.<name>.merge` left the lease passing, the push overwriting an existing branch, and the receipt announcing a **creation** — the least alarming reading available, on the operation that destroys work irrecoverably. The line is now read off git's own `--porcelain` per-ref summary, which separates all four outcomes: `[new branch]` → `(branch created)`, `<old>..<new>` → `Remote <old> → <new> (the branch already existed on the remote)`, `<old>...<new> (forced update)` → the same with `force-updated`, `[up to date]` → `already up to date, ref unchanged`. When git reported nothing readable it says `⚠ Remote now at <sha> — what it pointed at BEFORE this push is UNKNOWN`, names why, points at `git reflog show <remote>/<branch>`, and carries `PRE-PUSH REMOTE STATE UNKNOWN` to the `[result]` line. **The two remaining silent checks in the receipt speak up when they fail** ([#662](https://github.com/Digital-Process-Tools/claude-supertool/issues/662)): the ahead/behind block guarded on `returncode == 0` with no `else`, and an in-sync push legitimately prints nothing there, so a check that could not run was indistinguishable from one that ran and found agreement — it now prints `⚠ vs upstream: UNKNOWN — <command> exited N — <why>`. The uncommitted-leftovers check never read the return code at all, so a failed `git status --porcelain` gave empty stdout, an empty list and therefore silence, which in this receipt means *clean tree* — exactly the run on which "did I forget to commit something?" matters most. It now prints `⚠ UNCOMMITTED-CHANGES CHECK DID NOT RUN — <why>` and says in plain words that it is **not** claiming the tree is clean. Both keep their silence in the working case, so silence stays a positive claim. **The timeout advice names the budget that actually cut** ([#663](https://github.com/Digital-Process-Tools/claude-supertool/issues/663)): it used to say *raise `ops.git-push.timeout` in `.supertool.json`*, which cannot lengthen `_PUSH_TIMEOUT` — a constant in `presets/git/push.py` read from no config — so the advice was followed, nothing changed, and the caller went looking for a slow network instead. Naming a knob that does not govern the thing that cut is a confidently wrong disclosure and worse than silence, because silence does not lie ([#633](https://github.com/Digital-Process-Tools/claude-supertool/issues/633)). The receipt now names `_PUSH_TIMEOUT`, states that the op-level cap alone will not move it and why the two must stay ordered, and points at the lever that works on that path: the push **landed**, so re-running `git-push` prints the full receipt with no configuration change at all. **No post-push check can cost you the receipt** ([#675](https://github.com/Digital-Process-Tools/claude-supertool/issues/675)): every helper the receipt calls after the push has landed runs under a 30s `subprocess.run` timeout, and each one that called it bare turned a `TimeoutExpired` or an `OSError` into a stack trace out of `main()` — for a push whose remote had already moved, so the caller got a traceback and no `[result]` and read it as "the push blew up". All of them now go through one three-state helper: the check answered, the check found something, or it **could not run** and says so by name (`⚠ STALE-BASE CHECK DID NOT RUN — …`, `⚠ UPSTREAM LOOKUP DID NOT RUN — …`, `⚠ vs upstream: UNKNOWN — …`), because a per-helper guard returning `""` stops the traceback and re-introduces the silence it was fixed for. Above that sits a receipt-level guarantee that does not depend on the next call site remembering: whatever raises, the op prints the exception **and** a verdict, and a crash after the push landed reports `PUSHED … (RECEIPT INCOMPLETE — …)` with exit `0`. Two quieter defects on the same path went with it — `_live_remote_sha`, the one helper that *was* guarded, caught `TimeoutExpired` only and let an `OSError` through; and the verdict decided `verified` from `head and live == head`, so a `git rev-parse HEAD` that never answered was reported as `verified, but remote != local HEAD` — a **claim of divergence built out of an absence**, on the one line [#623](https://github.com/Digital-Process-Tools/claude-supertool/issues/623) exists to make you read. **The remote for a first push is resolved, not assumed** ([#656](https://github.com/Digital-Process-Tools/claude-supertool/issues/656)): on a branch with no upstream the op reads `branch.<name>.pushRemote`, then `remote.pushDefault`, then `branch.<name>.remote` — git's own precedence order for a bare `git push`, so it targets whatever `git push` would — then `origin` if such a remote exists, then the only remote if there is exactly one. `git clone -o gitlab` and fork/`upstream`-only layouts used to die on `fatal: 'origin' does not appear to be a git repository`, on the one push where `-u` is the point. With **two or more remotes, none named `origin`, and nothing configured, it refuses**: `ERROR: cannot determine which remote to push <branch> to — this repository has N remotes and none of them is named origin: <names>`, exit `1`, nothing pushed. Guessing wrong on a first push does not fail, it *succeeds* — creating a branch on a remote you never named, plausibly a public one, and pointing every later push at it — so this is a decline rather than a pick (see "Declining instead of guessing" in `docs/validators.md`). To settle it, either `git push -u <remote> HEAD` once, or `git config branch.<name>.remote <remote>` and re-run `git-push`; a repo with **no** remotes at all, and a `git remote` that could not be asked, each say so in their own words rather than sharing one message. **A branch made with `git worktree add -b <branch> <path> origin/master` is not "no upstream" either, and used to be misreported the same way `git-push` had already learned not to** ([#787](https://github.com/Digital-Process-Tools/claude-supertool/issues/787)): `branch.autoSetupMerge` tracks the *start point*, so `@{upstream}` resolves to `origin/master` even though the branch has never been pushed. A bare push there hands the target to `push.default`, which refuses on the name mismatch, and the refusal used to render as `NOT PUSHED - REJECTED  branch -> origin/master` — a target `push.default` picked, not the caller, and a verb implying the remote had acted when the push never reached it. `git-push` now detects `has_upstream and remote_ref != branch` — the exact precondition for that fatal — before invoking `git push` at all, and declines: `NOT PUSHED - no push attempted (<branch>'s upstream is <remote>/<ref>, a different branch — ambiguous target, nothing pushed)`, naming both remedies (`git push -u <remote> HEAD` to push under the branch's own name, or `git push <remote> HEAD:<ref>` to push onto the tracked branch on purpose) |

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
./supertool 'git-resolve:::both:::CHANGELOG.md'
./supertool 'git-commit:::Merge master into feature/auth'
```

`both` is a union, so it is refused on source files — `git-resolve:::both:::all`
resolves the changelog and holds back the code, which stays conflicted:
```text
# git-resolve: both (2 file(s))
  ✓ CHANGELOG.md
      markers: clean | validate: ok
  ⊘ src/app/Config.py: source file — 'both' concatenates both versions (the result parses; the code runs twice); refused

Resolved: 1 | Refused: 1 | Failed: 0 | Remaining: 1
Next: resolve these by hand — ./supertool 'git-conflicts' to inspect, ./supertool 'git-resolve:::ours:::PATH' / ':::theirs:::PATH' to take one side,
      or append 'force' (git-resolve:::both:::PATH:::force) to union anyway and verify the result yourself.
```
A `CHANGELOG.md` is the one file `both` is usually *right* about — which is why the
other refusal is worth knowing before you meet it mid-rebase. When one side has had a
release section cut above the entry both branches touched, the release heading sits
inside the hunk on both sides and the union emits it twice, quietly reparenting the
unreleased entries under it:
```text
# git-resolve: both (1 file(s))
  ⊘ CHANGELOG.md: structured document — union would duplicate 1 heading(s) present on both sides: '## [0.23.0] - 2026-08-05', reparenting the lines between the two copies under the first; refused

Resolved: 0 | Refused: 1 | Failed: 0 | Remaining: 1
```
Resolve those two sections by hand, or `force` it and check the headings yourself.
Only the hunk's *own* headings count, so the everyday case — two branches each adding
a bullet under the same `### Fixed` — still unions without a word.

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

**`markers: clean` on its own means the parser had nothing to say, never that it
did not run** ([#880](https://github.com/Digital-Process-Tools/claude-supertool/issues/880)).
A validator that matched the file and then declined — `php` not installed, an
adapter that timed out — says so on the same line, and a decline beside a pass
still costs a word:
```text
  ✓ src/app/Config.php
      markers: clean | validate: ⚠ not checked (phplint (php not installed))
  ✓ src/app/other.php
      markers: clean | validate: ok | ⚠ not checked by phpstan (php not installed)
```
Nothing is blocked either way — the digest is advisory — but `markers: clean` is
the phrase a reader uses to decide not to look, so it may not stand alone over a
file nobody parsed.

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

### What `git-commit` did not do

Two renders on this op used to stop one line short of the thing the reader needed, both in the direction where silence reads as completeness.

**A partial commit now names what it left behind** ([#1016](https://github.com/Digital-Process-Tools/claude-supertool/issues/1016)). `git-commit:::MSG:::PATHS` prints a `✓` and `Files committed: N`, which argues nothing was omitted. When modified tracked files are still uncommitted afterwards, they are listed:

```
Files committed: 5
⚠ 2 modified tracked file(s) were NOT included:  (3 untracked, not listed)
    presets/git/commit.py
    tests/test_commit.py
  Intentional? If not: git-commit:::MESSAGE:::presets/git/commit.py:::tests/test_commit.py
```

Named, not counted: "2 not included" costs a second call to find out which, and a reader who has to make that call usually does not. Untracked files are counted only — nearly every worktree has some, and a list of them under every commit is a list nobody reads on the commit that needed it. A run that left nothing behind prints none of this, and a check that could not run says `SKIPPED` rather than nothing.

**A refusal on an unstaged tree now names what is unstaged** ([#1003](https://github.com/Digital-Process-Tools/claude-supertool/issues/1003)). `ERROR: nothing staged.` was correct and unhelpful: the op had just read the working tree and the caller's only remaining move was a raw `git add -A`, i.e. the command this op exists to replace. The refusal itself stays — committing files you did not name is not a default anyone wants — but it now lists the modified tracked and untracked paths separately, and hands back a `git-commit:::MESSAGE:::…` call naming the first few. A genuinely clean tree says so instead, and a `git status` that did not answer says *that*, rather than printing an empty list that reads as clean.

### Which repository an op acted on

### A commit message containing `:` needs the triple-colon or `@payload` route

`git-commit:fix(rector): link the importer` does not work, and cannot: supertool's
single-colon CLI splits on **every** `:`, so that message arrives as
`MESSAGE='fix(rector)'` plus a PATH of `' link the importer'`. Every Conventional
Commits subject has this shape.

Use either documented route instead:

```bash
./supertool 'git-commit:::fix(rector): link the importer'
./supertool 'git-commit:::fix(rector): link the importer:::src/importer.py'
```

```bash
./supertool 'git-commit:@-' <<'EOF'
message = '''fix(rector): link the importer

Co-Authored-By: Max <noreply>'''
paths = ["src/importer.py"]
EOF
```

When a payload will not load — a plain message file, a mistyped key, an `@`
reference left in the colon slot — the refusal now names the keys the op wants
and prints a call that would work, derived from the same registry that drives
the route ([#1003](https://github.com/Digital-Process-Tools/claude-supertool/issues/1003)).
Three agents in one evening each guessed `message` and `paths` from scratch off
a bare TOML line-and-column, and one mangled its commit message past the shell
instead — permanently, in that history.

Since [#751](https://github.com/Digital-Process-Tools/claude-supertool/issues/751)
the split shape is **refused before anything is staged**, with the reconstructed
message handed back in both forms — instead of failing downstream as
`git add failed: fatal: pathspec ' link the importer' did not match any files`,
which named neither the message nor the tokenizer.

The trigger is narrow: a PATH argument that is neither path-shaped (no whitespace,
no quotes) nor known to git (on disk, tracked, or an already-staged deletion). A
path-shaped PATH that git simply does not know — a typo, a file not created yet —
still gets git's own pathspec error, because supertool has no basis to decide it
was really message text. Nothing is ever folded back into the message: guessing
wrong in that direction would commit the already-staged fileset under a mangled
subject and print a success receipt for it.

Every git op runs against the repository discovered from the **current working directory**, and says which one that was. `git-diff`, `git-commit` and `git-push` each stamp a `Repo: <toplevel>` header (a bare repo reports its git dir, marked `(bare)`); `git-commit` prints it before staging, so it is on the receipt even when a hook rejects the commit or nothing was staged.

That is not a formality. Git's `GIT_DIR`, `GIT_WORK_TREE` and five siblings override discovery-from-cwd, and git exports them to every hook it runs — so a supertool call made from inside a git hook inherits a pointer to whatever repo invoked the hook. Before [#692](https://github.com/Digital-Process-Tools/claude-supertool/issues/692) that silently retargeted the op, and the receipt named no repository to contradict it.

Those seven variables are removed **once per call, before any op dispatches** — see [Inherited `GIT_*` environment](../operations/meta.md#inherited-git-environment), which is where the boundary and its reasoning are written down. The scrub is not a property of the git preset: it covers every op, preset and built-in alike.

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
[result] NOT PUSHED - no push attempted (fix/x's upstream is origin/master, a different branch — ambiguous target, nothing pushed)
```

**A branch's first push, after `git worktree add -b`, is not "no upstream" and does not read as a rejection any more** ([#787](https://github.com/Digital-Process-Tools/claude-supertool/issues/787)). `git worktree add -b <branch> <path> origin/master` — the normal way to start a fresh branch — leaves `@{upstream}` resolving to `origin/master` via `branch.autoSetupMerge`, tracking the *start point* rather than the new branch. A bare `git push` there hands the target to `push.default`, which refuses on the name mismatch; before the fix that refusal rendered as `NOT PUSHED - REJECTED  branch -> origin/master` — a target `push.default` picked, not the caller, and a verb implying the remote had acted when the push was never attempted. `git-push` now detects that precondition before invoking `git push` at all and declines instead, naming the phantom upstream it found and both of git's own remedies:
```
Nothing was pushed. Name the target once:
  ./supertool 'git-push:set-upstream'   # push fix/x under its own name, tracking origin/fix/x (the usual first push)
  ./supertool 'git-push:to-upstream'    # push onto origin/master on purpose, if that is the real target
```

**Both remedies are flags on this op, because the ones it used to print were commands the caller could not run** ([#879](https://github.com/Digital-Process-Tools/claude-supertool/issues/879)). The refusal above is correct and stays exactly as strict — you still have to say which of the two intents you meant, which is the whole reason it declines. What changed is the vocabulary it answers in. It used to prescribe `git push -u origin HEAD`, and in the project this op was built for raw `git push` is blocked by a hook — a hook that exists *because* `git-push` is better than raw git. Two correct rules composed into a dead end, and the way out that got used (`git branch --unset-upstream`, then re-run the op) is git trivia that happens to work, which is how a gap like this survives for weeks instead of being filed.

`:set-upstream` pushes the branch under its own name and repoints tracking at `<remote>/<branch>`. The receipt says what it retargeted away from, rather than rendering the branch as if it never had an upstream:

```
Upstream: origin/master — inherited, not this branch. :set-upstream retargets it to origin/fix/x (configured in branch.fix/x.remote)
```

`:to-upstream` pushes onto the tracked ref on purpose, with an explicit `HEAD:<ref>` refspec — never a bare push, since a bare push is precisely the input `push.default=simple` refuses. The header names the target so a `to-upstream` onto a shared branch cannot be mistaken for an ordinary push. Asking for **both** is refused (exit 2, nothing pushed) naming the two refs: they are different destinations, and ordering them by precedence would be the guess the refusal exists to prevent.

**A remote-tracking ref named like a git option can no longer reach `git fetch` as one** ([#818](https://github.com/Digital-Process-Tools/claude-supertool/issues/818)). On git 2.46.2, `git fetch origin '--upload-pack=<cmd>; git-upload-pack'` *executes* `<cmd>` — and the `(remote, ref)` pair `git-push` (non-fast-forward recovery) and `git-merge` (freshest-ref fetch) hand to `fetch` is split from `@{upstream}`, i.e. a remote-tracking ref name anyone who controls the remote can choose. A branch tracking `origin/--upload-pack=…` fed that value straight through as a bare argv element — no `--`, no leading-dash refusal — where the argv-`REF` guards on `git-checkout`/`git-merge` ([#150](https://github.com/Digital-Process-Tools/claude-supertool/issues/150)) never saw it. Both sinks now refuse a `remote`/`ref` that begins with `-`, **by name** (a silently dropped ref would fetch the wrong thing): `git-push` aborts the recovery; `git-merge` skips the fetch and merges the local ref — safe, since only the fetch runs the payload. `git-ls-remote`'s `--upload-pack` does not execute and is unaffected.

**Platform scope, stated rather than implied.** The execution is proven on POSIX (macOS, Linux, git 2.46.2). Whether it also executes on **Windows is not established** — do not read this note as claiming it does. The guard itself is a leading-dash string check with no platform-dependent behaviour, so it closes the sink either way. The test suite carries a positive control that runs the unguarded fetch and proves the sink is live on whatever platform it runs on; the "payload did not run" assertions depend on it, since that assertion passes for free where the payload could never have run. The refusal assertions run everywhere regardless.

**A push that landed still ends on a verdict even when the receipt itself breaks** ([#675](https://github.com/Digital-Process-Tools/claude-supertool/issues/675)). Every check that runs *after* the push is past the point of no return, so none of them may cost you the answer:
```
[result] PUSHED  feat -> origin/feat @ unknown  (RECEIPT INCOMPLETE - git-push crashed after the push landed: OSError: ...)
```
The exception is printed in full above it, on stdout — swallowing it would trade a loud failure for a quiet one — and the exit code follows the **push**, not the receipt, so a landed push never reports as a failed one.

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
To **cancel** and get back to exactly where you were before the push (your commits intact, nothing pushed), `git rebase --abort`. To overwrite the remote intentionally (your history wins), `git rebase --abort` then `./supertool 'git-push:force-with-lease'`. To skip a local pre-push hook that diverges from CI, `./supertool 'git-push:no-verify'`. To make a branch's **first** push when `git worktree add -b <new> <base>` left it tracking `<base>`, `./supertool 'git-push:set-upstream'` — see the `:set-upstream` / `:to-upstream` note below.

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

## Occupancy has three states, and `idle` is the one that must be earned

`git-worktrees` exists because of one afternoon ([#860](https://github.com/Digital-Process-Tools/claude-supertool/issues/860)). To decide whether a worktree was free, the maintainer ran:

```
$ ps aux | grep -c "st-wt/804"
0
```

and reported the agent dead, *verified*. It was alive. **A worktree path is not in that process's argv — the process is `chdir`'d there.** The zero was a fact about the pattern, not about the tree. A second agent was delegated in, and for two minutes both wrote through one index: files changing between one agent's own calls, a `reset` in the reflog that nobody issued, a `batch` anchor 93 lines from where it had been. Nothing was lost, which was luck.

So the verdict has three states and the third one is the point:

| state | what it means | what licenses it |
|---|---|---|
| `occupied` | somebody is in there | any **positive** signal: `index.lock`/`HEAD.lock`, a rebase/merge/cherry-pick in progress, a `git worktree lock`, a write inside the activity window, or a process whose **cwd** is inside the tree |
| `idle` | safe to use | **every** probe answered: the tree was stat'd and has been quiet for `SUPERTOOL_WORKTREE_IDLE_QUIET` (default 1h), **and** the process table was read and holds nobody |
| `cannot tell` | undecided — treat as occupied | anything else, naming which probe went silent |

**`idle` is not the default; it is earned.** A checker that resolves uncertainty towards `idle` reproduces the incident exactly, because "no evidence of an agent" is precisely what the `ps` grep already said. The only probe that can license an absence is the one that positively looks for it — the process-cwd scan — and where that scan cannot run, the answer is `cannot tell`, never `idle`.

**The signals, and why none of them is sufficient alone.** `index.lock` is proof when present and absent between any two operations of a busy agent, so its absence refutes nothing. Recent mtimes catch an agent that is writing and miss one that is thinking. A `git worktree lock` is an *announcement* — git's own, so it is read rather than reinvented — but only a tool that already knows about it writes one. The cwd scan is the one that answers the question the `ps` grep asked wrong, and it is platform-divergent: `/proc/<pid>/cwd` on Linux, `lsof -a -d cwd` elsewhere, and **neither on Windows**, where the op declines by name and says so rather than degrading to a verdict. An `lsof` that stalls or prints nothing is `unknown`, never "found nobody" — that is the same bug one layer down.

**Why `idle` needs two windows, not one.** Written against the live fleet, the first run found two worktrees edited 7 and 12 minutes earlier in which **no process had its cwd inside** — an agent's parent process does not have to be `chdir`'d into the tree it is editing. So "the process table was read and holds nobody" is a far weaker licence than it looks: on its own it would have declared a working tree free at the sixteenth minute. `occupied` therefore uses the short window (`SUPERTOOL_WORKTREE_ACTIVE_WINDOW`, 15m — is somebody writing *now*), and `idle` uses a long one (`SUPERTOOL_WORKTREE_IDLE_QUIET`, 1h — has this tree been abandoned). **Between the two the answer is `cannot tell`**, and it says why: *quiet for less than 1h, and an empty cwd scan does not prove absence*.

**Inference, not announcement — and what the alternative would have cost.** A claim file written by the occupant on entry is the stronger-looking design and was rejected: nothing already running writes one. The five agents live in sibling trees on the afternoon this shipped announced nothing, so a claim-based checker would have reported every one of them as unclaimed — the same false all-clear in new packaging. A claim also fails towards `occupied` forever when its writer dies, so it needs an age *and* a liveness cross-check, which is this op again, underneath it. Inference works on occupants that never heard of the mechanism, which is the population that caused the damage.

**Inspection only, on purpose.** Nothing here removes, prunes, unlocks or writes, and the report never prints a `git worktree remove` command. `git worktree remove` refuses while a branch is checked out and after a merge lands, so "which of these can I clean up" is worth *answering* — hence the `merged` marker on each row — but a destructive suggestion sitting underneath an ambiguous verdict is how an occupied tree gets removed.

**The board is the tool's; the cells in it are not.** A `st-wt/NNN` worktree exists to hold somebody else's branch, so its filenames, its path and its refnames are text this repo did not write — and one of them is printed, because the evidence names the newest write. A filename may contain a newline, and unflattened one file called `a.md␊idle          main                       ~repo  [merged]` rendered **a whole extra worktree row carrying an `idle` verdict** ([#876](https://github.com/Digital-Process-Tools/claude-supertool/issues/876)) — the one verdict that gets a tree deleted. So the render guarantees its own shape: **a row is one line, plus one line per piece of evidence, whatever it is handed.** Branch, path and every filename are flattened, and nothing they contain can reach column 0, add a line, imitate a column gap with a tab, or move the cursor back over a line already printed. Nothing is censored — a control character is shown as itself (`␊`, `␛`) and every other character survives on the line it was given, because the reader is an agent that has to act on the path and an unreadable path is its own failure. The board says so in one line under its heading rather than fencing every row.

**Exit codes carry the same three states.** With a `PATH`: `0` for `idle` and only `idle`, `1` for `occupied`, `2` for `cannot tell`. A caller that tests `== 0` therefore gets the safe reading of an undecided answer. Without a `PATH` it lists every worktree and exits `0` — the tally is in the `[result]` line, which says in words that `cannot tell` is not `idle`.

## The tracker column has four states, and `unknown` is one of them

The board told you who was *in* a worktree and never what that worktree's work was *worth* — so deciding where to act next meant running `git-worktrees`, then `gh-prs`, then `gh-pr:N:status` twice, and joining branch → path → PR number → check tally in your head ([#941](https://github.com/Digital-Process-Tools/claude-supertool/issues/941)). Every one of those joins is a fact the tool held on both sides.

Each row now ends in a tracker token, and its reasoning is one more evidence line:

```
cannot tell  fix/910-879     ~/Documents/st-wt/910   PR #937
             · no positive signal — so this declines rather than reporting the tree free
             · PR #937 → master · 20 total: 20 passed, 0 failed, 0 pending · MERGEABLE
occupied     fix/941         ~/Documents/st-wt/941   no remote ref
             · reflog written 2m ago (inside the 15m activity window)
             · no remote-tracking ref for this branch here — it was either never pushed, or
               its remote branch has been deleted (the usual state after a merge)
```

| token | what it means |
|---|---|
| `PR #N` | an open PR tracks this branch; the tally is `_checks.summarize()`, the same summed arithmetic `gh-pr:N:status` prints, so a `CANCELLED` leg is a named term and not a silent zero |
| `no open PR` | GitHub answered and holds no open PR for a branch that **is** pushed — published, unproposed |
| `no remote ref` | no remote-tracking ref here: never pushed, **or** the remote branch was deleted after a merge |
| `PR unknown` | the lookup did not run, and this is not a statement about the world |
| `PR n/a` | the worktree has no branch (detached or bare) — nothing to look up |

**`PR unknown` versus `no open PR` is the whole design.** They are the tool's absence and the world's absence, and this repository has paid for that confusion more than for any other single mistake. Rendered as one state they read *this work is unpublished, take the tree* — in the op whose entire job is deciding which tree to take, at exactly the moment (offline, expired token, rate limit) when you are least able to check. So a lookup that did not answer is consulted first, before any local fact, and it says which failure it was.

**A page that hit its cap is `unknown`, not `no open PR`.** One `gh pr list --limit 100` serves every worktree, so N trees cost one call rather than N. But if the page comes back *full*, it is authoritative for the branches it names and establishes nothing about the ones it does not — the partially-answered batch is state 4 wearing state 2's clothes.

**"Never pushed" is not claimed, because it cannot be observed.** The first live run of this column called four `[merged]` worktrees "never pushed"; all four had been pushed, merged, and deleted on the remote. A deleted remote branch and an unpublished one leave an identical local trace, and `branch.<name>.merge` does not separate them either — `git worktree add -b X … master` writes an upstream of `origin/master` for a branch that has never left the machine. The row reports the observation and names both histories.

**It is on by default, and that is a deliberate cost.** Before #941 this op made no network call at all. It now makes exactly one, on an 8s budget, adding roughly a second. Opt-**in** was the alternative and was rejected: the friction being fixed is a join done in the reader's head, and a suffix only helps a reader who already knows the suffix exists, which is not the one reaching for this board at speed. A failed call degrades to a stated `unknown` rather than to a wrong answer, so the downside is bounded. `nopr` — or `SUPERTOOL_WORKTREE_PR=0` — restores the fully offline op, and the `[result]` line counts the rows whose tracker did not answer so a missing answer survives `| tail -1`.

**The exit code is untouched.** It remains a statement about occupancy alone: a tracker that could not be read says nothing about whether a tree is safe to enter, and folding it in would make `git-worktrees:PATH` refuse a free worktree because GitHub was down.

## Configuration

Most ops need no project config. `git-investigate` takes two env vars (below); `git-diff` takes optional review policy (see **git-diff policy** below).

| Variable | Default | Effect |
|----------|---------|--------|
| `SUPERTOOL_COMMITS` | `10` | Number of recent commits to show |
| `SUPERTOOL_WORKTREE_ACTIVE_WINDOW` | `900` | `git-worktrees`: a write newer than this many seconds reads as somebody working (→ `occupied`) |
| `SUPERTOOL_WORKTREE_IDLE_QUIET` | `3600` | `git-worktrees`: a tree must be quiet at least this long before `idle` is allowed at all; between the two windows the answer is `cannot tell` |
| `SUPERTOOL_WORKTREE_PR` | `1` | `git-worktrees`: set to `0`/`false`/`no`/`off` (or pass `nopr`) to drop the tracker column and make the op fully offline |
| `SUPERTOOL_BLAME_RECENT` | `5` | Number of blame hotspot lines to surface |
| `SUPERTOOL_GIT_TIMEOUT` | `10` (`5` for `git-status`) | Seconds each individual git call gets before it is abandoned and disclosed (see **When git does not answer**) |

Set via the op's JSON config if you want project-wide defaults:
```json
{
  "ops": {
    "git-investigate": { "cmd": "python3 {path}git/investigate.py {arg}", "commits": 20 }
  }
}
```

`git-status` and `git-push` try `glab` then `gh` to surface the open MR/PR — skip gracefully if neither is installed. A CLI that *is* installed and cannot answer is a different state and is disclosed rather than skipped; see below.

### The git-call budget

Every git call any of these ops makes gets `SUPERTOOL_GIT_TIMEOUT` seconds — default **10**, except `git-status`, which keeps **5** because every call it makes is a courtesy line on a report you want back fast and none of them writes anything.

Three calls name their own budget instead, because they are the ones that legitimately take longer, and a module-wide default large enough for them would be far too generous for the `rev-parse` plumbing around them:

| call | budget | why |
|---|---|---|
| `git push` (`git-push`) | 300s | The op owns its own timeout so it can verify the remote before reporting; supertool's outer cap must not fire first |
| `git fetch` / `git rebase` on `git-push`'s recovery path | 120s | Can land on a worktree git has already paused ([#640](https://github.com/Digital-Process-Tools/claude-supertool/issues/640)) |
| `git commit` (`git-commit`) | 30s | Runs whatever the pre-commit hook chain is |
| `git merge` (`git-merge`) | 30s | Runs merge drivers, potentially over the whole tree |

**An explicit budget wins; the environment sets the default** ([#704](https://github.com/Digital-Process-Tools/claude-supertool/issues/704)). Setting `SUPERTOOL_GIT_TIMEOUT=5` to tighten `git-status` does not cap `git-push`'s 300s and report a push still in flight as failed.

**What changed in [#704](https://github.com/Digital-Process-Tools/claude-supertool/issues/704).** Ten of these presets each carried their own copy of the git wrapper, and the copies had drifted to three different budgets — 5, 10 and 30 — that nobody had chosen together and no test pinned. They now share one, so the numbers above are the whole story rather than a summary of ten. Two consequences worth knowing before you upgrade: the plumbing calls in `git-commit` and `git-push` (`rev-parse`, `diff --cached`, `rev-list`) drop from a 30s budget to 10s, and `SUPERTOOL_GIT_TIMEOUT` now reaches `git-checkout`, `git-diff`, `git-diverge`, `git-trail`, `git-investigate`, `git-blame`, `git-merge`, `git-resolve`, `git-commit` and `git-push`, which previously ignored it entirely.

### When git does not answer

A call that does not come back inside its budget is abandoned rather than waited out, and it costs its own section instead of the whole report ([#650](https://github.com/Digital-Process-Tools/claude-supertool/issues/650)). Previously the `TimeoutExpired` escaped: one stalled `rev-list` — the courtesy line about divergence from master — replaced branch, commits, working tree, stashes and PR with a stack trace.

That fix landed in `git-status` and nowhere else, because the wrapper existed ten times ([#704](https://github.com/Digital-Process-Tools/claude-supertool/issues/704)). `git-conflicts` kept the defect for two more releases and printed `No conflicted files.` and exit 0 over live `<<<<<<<` markers. Every op listed on this page now behaves the way the paragraph above describes.

An abandoned call is never rendered as a git that succeeded and printed nothing. It carries exit code `124` (the shell convention for "killed by a timeout"), so every call site's existing "did this work?" branch skips its section exactly as it would for a git that failed. Without that, an empty `rev-list --left-right --count` reads as `0 ahead — branch has no own commits!` and an empty `rev-parse --abbrev-ref HEAD` prints `Branch:` with nothing after it — a false alarm about the branch manufactured out of a fact about the machine.

**A skipped section says so where the section belongs, not only in the footer** ([#1002](https://github.com/Digital-Process-Tools/claude-supertool/issues/1002)). `git-status`'s working-tree and stash sections used to be omitted entirely when their call did not answer, and the footer below carried the only disclosure. A reader who scrolls to where the working tree should be and finds nothing has already concluded the tree is clean — the footer arrives after the decision. Both sections now render three states, and the third is a marker in place:

```
## Working tree: clean
## Working tree (7 changes)
## Working tree: UNKNOWN — `git status --porcelain=v1` did not answer (exit 128: fatal: unable to read index). This run did not look — it is not 'clean'.
```

The footer stays as well, deliberately. The marker is where the eye is; the footer is the line that survives a `| tail` and the one a script greps. Neither is printed on a run where every call answered, so the common case gains nothing to skim past.

The missing sections are then named rather than left to look like nothing-to-report, each with the reason it could not answer:

```
git-status INCOMPLETE — 2 calls did not answer and were skipped: `git status
--porcelain=v1` (exit 128: fatal: Unable to create index.lock: File exists),
`glab mr view` (exit 1: error: 401 Unauthorized). Sections that depend on them
are missing because the call did not answer, not because there was nothing to
report. Raise SUPERTOOL_GIT_TIMEOUT if a timeout recurs.
```

The line appears last, and only when there is something to disclose — a clean run carries no permanent disclaimer, which would disclose nothing. Raising the budget is the right move on a loaded or slow runner; it is a property of that machine, never of the repo being reported on.

**A timeout is not the only way a call fails to answer** ([#705](https://github.com/Digital-Process-Tools/claude-supertool/issues/705)). The footer covers three more sources, all of which previously rendered as an answer:

| what happened | used to read as | now |
|---|---|---|
| `glab`/`gh` stalled, refused, or returned non-JSON | *this branch has no MR* — identical output | footer names the CLI and quotes its own words |
| `git status --porcelain=v1` failed (a held `index.lock` is enough) | no working-tree section, as though the report simply had none | footer names the call |
| `git stash list` failed | no stash section | footer names the call |

The reason travels with the command because "did not answer" alone sends a reader to raise `SUPERTOOL_GIT_TIMEOUT` for what is actually an expired token.

**"There is no MR on this branch" stays silent, and that is deliberate.** `glab` and `gh` exit `1` both for that and for an expired token, so the exit code cannot separate them and the message has to. The phrases meaning a genuine absence — *no open merge request*, *no pull requests found*, and the other host's CLI reporting *none of the git remotes … point to a known GitLab host* — render as silence, because a branch with no MR yet is the most ordinary state a branch can be in and a footer on every such run is one nobody reads on the run that needs it. Anything unrecognised is disclosed instead of assumed benign: that costs one footer line quoting the CLI, which a reader dismisses in a second, where the opposite error is the defect itself. A CLI that is not installed stays silent — nothing on that machine was going to answer, and a decline that can never resolve is noise on every run.

### `git-push`'s MR/PR line has the same three states, and got them last

`git-status` learned this in [#705](https://github.com/Digital-Process-Tools/claude-supertool/issues/705); the shared branch→MR lookup `git-push` uses did not, until [#948](https://github.com/Digital-Process-Tools/claude-supertool/issues/948). It returned `None` for *there is no open MR/PR* and for *the lookup did not happen*, and swallowed every exception on the way — on the op a person reads immediately before and after the one irreversible thing supertool does.

Three consumers read that `None`, and all three took it as an absence:

| what the receipt did | with a real absence | with a lookup that failed |
|---|---|---|
| the `MR !42 → master` line | correctly absent | absent — reads as "no MR yet, open one" |
| the mergeability warning and the stale-base check | correctly skipped (no target branch) | skipped, silently — a conflicting MR raised nothing |
| `:watch` | *"there is no open MR/PR for this branch yet — nothing to watch. Open one"* | the same sentence, about a request that may well exist |

Now the lookup answers with a fact or with the reason there is no fact:

```
⚠ MR/PR LOOKUP DID NOT RUN — `gh` timed out after 5s
  Whether this branch has an open MR/PR is UNKNOWN — this receipt is not saying
  there is none, and the mergeability and stale-base checks below are missing
  for the same reason. Settle it: ./supertool 'git-status'
```

**It does not block the push, and that is the deliberate half.** This runs after the ref has moved. A tracker that cannot be reached is not a reason to withhold work already on the remote, and refusing there would trade a quiet wrong answer for a loud one that is worse. Degrading to a stated unknown is the whole fix.

The first thing it disclosed was a defect of its own: `glab mr list --state opened` is rejected by glab 1.86 (`Unknown flag: --state.`, exit 1), so the GitLab arm had been failing at argument parsing on every call and falling through to `gh`, which cannot answer on a GitLab repo either. Both failures were swallowed, so nothing said so. Open is `mr list`'s default, and the flag is gone.

**The healthy path is byte-identical to before.** A lookup that answered prints exactly what it printed, and a branch that genuinely has no MR still says nothing — a line on every call is a line nobody reads on the call that needed it. Three cases are answers rather than failures, the first two on the same reading `git-status` uses: a CLI reporting *no open merge request* / *no pull request*; the other host's CLI reporting *none of the git remotes … point to a known GitHub host*; and — read from `git remote -v` rather than from a CLI — a repo where no remote names a host at all, so there is no tracker a request could be open on. That last one is checked first and locally, because both CLIs verify their own credentials before they look at the remotes: an unauthenticated `gh` exits `4` about `GH_TOKEN` and never gets as far as the sentence that would have made this an answer. The second is structural and permanent, so it must not decline — otherwise every push in every GitLab repo with `gh` installed carries a warning. An answer already given is never downgraded by the fallback either: `glab` saying "no MR" on a GitLab repo stands, whatever `gh` does a moment later.

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
| `forbidden_paths` | `[{pattern, reason}]` | Warn when a changed file's path matches `pattern` (regex); `reason` is printed verbatim. **Added to the shipped secret-shaped defaults, not a replacement for them** — see below. |
| `test_pairing` | `[{src, test}]` | For each **added** source file matching `src` (regex with named groups), warn if the derived `test` path exists neither in the diff nor on disk. `test` is a template — `{name}` placeholders are filled from `src`'s named captures. |
| `hints` | `[{added, message}]` | Print `message` once if any **added** path matches `added` (regex) — for follow-up reminders. |
| `red_flags_extra` | `[{pattern, ext?, label}]` | Extra added-line red flags on top of the defaults. `pattern` (regex) is tested per added line; optional `ext` (e.g. `.js`) scopes it to one extension; `label` names the hit. |

**The forbidden-path guard ships with rules** ([#693](https://github.com/Digital-Process-Tools/claude-supertool/issues/693)). It used to hold only what `forbidden_paths` gave it, so a repo that had configured nothing ran the guard over an empty rule set and got an affirmative `✓ … no forbidden paths` for free — a `.env` and an `id_rsa` passed review that way. `DEFAULT_FORBIDDEN_PATHS` now covers secret-shaped names: `.env` (but not `.env.example`/`.sample`/`.template`/`.dist`/`.defaults`), `id_rsa`/`id_dsa`/`id_ecdsa`/`id_ed25519` (but not the `.pub` halves), `*.pem`/`*.pfx`/`*.p12`/`*.jks`/`*.keystore`/`*.key`, `.npmrc`/`.pypirc`/`.netrc`, `credentials.json`, `service-account*.json`, and anything under `.aws/`. They are heuristics on filenames — not a secret scanner, and not a substitute for one.

**Two related behaviours.** A policy value that cannot be parsed is now reported rather than treated as an empty one: a malformed `forbidden_paths`, `test_pairing`, `hints` or `red_flags_extra` prints under `⚠ Policy not loaded`, names the key, and states that its rules were not applied — previously a typo disabled a guard silently and the run still printed a clean verdict. And the clean verdict names only the checks that ran, so with no `test_pairing` configured it reads `✓ No red flags or forbidden paths.` rather than claiming a test-pairing check that had no rules.

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
