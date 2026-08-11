# git

Git investigation and workflow ops. Replaces the 4-6 raw `git` calls you'd normally chain to get a usable picture of a repo's state — `git status`, `git log`, `git diff`, `git blame`, `git log -S`, `git log --follow`. Each op packs the next question's answer into the current call so the agent doesn't need a follow-up turn to decide what to do.

## Requires

`git` installed and a git repository. No auth, no tokens.

## Ops

| Op | Syntax | What it returns |
|----|--------|-----------------|
| `git-status` | `git-status[:full\\|:brief]` | Branch, tracking, ahead/behind, last 5 commits, staged/unstaged/untracked files, stashes, open MR/PR link, suggested next step. The `Issue:` line reports only issues a GitHub closing keyword binds to (`Issue: #591`, `Issues: #571, #572`, or a stated `none declared`) — never the first `#N` in the body ([#591](https://github.com/Digital-Process-Tools/claude-supertool/issues/591), see [What `git-status`'s `Issue:` line claims](#what-git-statuss-issue-line-claims)). The default view caps each list (20 staged/unstaged, 10 untracked/branches, 5 stashes) with a `... (N more)` marker — cheap overview. `:full` (alias `:porcelain`) **uncaps every list** for the full untruncated view, e.g. when you need to drive precise staging (excluding a few pre-existing untracked items from a large commit) and can't from a truncated list. `:brief` goes the other way — it drops the local-branch inventory and the last-5-commits log so the working tree and the MR/PR block are near the top ([#1028](https://github.com/Digital-Process-Tools/claude-supertool/issues/1028)); see [`ahead N, behind M` after a rebase](#ahead-n-behind-m-after-a-rebase-is-not-lost-work). A mode that is none of these is named and the default render is labelled as such, rather than silently substituted |
| `git-investigate` | `git-investigate:PATH` | File history: recent commits touching the file, uncommitted changes, blame hotspots (most-recently-changed lines) |
| `git-trail` | `git-trail:PATTERN:PATH` | Trace a symbol or string through history via pickaxe search — when it was added, modified, or removed, with contextual diff hunks. **Both of its caps state themselves** ([#635](https://github.com/Digital-Process-Tools/claude-supertool/issues/635)): the `## Timeline` list is bounded by `SUPERTOOL_MAX_COMMITS` (default 20) and the `## Details` section renders at most `SUPERTOOL_TRAIL_DETAIL_CAP` commits (default 10) because it costs one `git show` each. When either bites, the marker is in the **header as well as** the footer — `[CAPPED: 10 of 47 commits shown by count — raise …]` — and it names a *count* limit, which is what actually cut, rather than a size budget. A capped timeline makes the detail denominator read `10 of 20+`, never a total nobody measured. **When nothing is cut, nothing extra is printed**, so an unmarked result is a positive claim that the trail is whole |
| `git-blame` | `git-blame:PATH:LINE[:N]` | Blame for N lines (default 5) around a specific line number |
| `git-checkout` | `git-checkout:REF` | Switch to branch/tag/SHA — reports tracking state, dirty files, last commits after switch. Its three recoveries for a ref this clone has not got — `fetch --all --prune`, `checkout -b --track <remote>/<ref>`, and an explicit `fetch origin <ref>` + `checkout -B <ref> FETCH_HEAD` for a narrowed refspec — all rewrite local state, so **they turn on `git rev-parse`'s exit code, not on git's error message** ([#649](https://github.com/Digital-Process-Tools/claude-supertool/issues/649)). That message is translated: under `LANGUAGE=fr` it reads `le spécificateur de chemin … ne correspond à aucun fichier connu`, and while the recoveries keyed on the English `pathspec` / `did not match any` they silently did not fire for anyone whose git speaks another language. Same channel discipline as `git-push`'s non-fast-forward decision. The remaining hints (`uncommitted changes block checkout`, `checked out in another worktree`) still read git's prose and so are still English-only; they choose wording, never an action. **`REF` must name a ref, and an argument that names a path is refused** ([#756](https://github.com/Digital-Process-Tools/claude-supertool/issues/756)). `git checkout <arg>` is two operations sharing one name: given a ref it switches branches, given a pathspec it restores those paths from the index — discarding uncommitted work with no reflog entry, no stash and no object written, so there is nothing anywhere to recover it from. Passing `git-checkout:work.txt` used to do exactly that and then report `Working tree: clean`, which was true only because the op had just made it so. Now an argument that git resolves as a path but not as a commit is refused before anything runs, naming what was passed and pointing at `git checkout -- <path>` for anyone who genuinely wanted the restore; and **every switch is issued as `git checkout <ref> --`**, so the pathspec reading cannot be selected even for an argument the check did not anticipate. There is deliberately **no pathspec-restore op**: discarding uncommitted work is not a mode of a branch-switch command. An argument that is *both* — a `docs` branch beside a `docs/` directory — resolves to the **ref**, always, and says so rather than leaving it to git's DWIM. |
| `git-diverge` | `git-diverge:BRANCH[:BASE]` | Branch vs base: ahead/behind counts, commit list, changed files, +/− line totals |
| `git-diff` | `git-diff[:staged\|:branch[:BASE]\|:PATH][:full]` | Review-aware diff (working / `staged` / `branch` merge-base / `PATH`): files grouped by kind + shortstat, red-flag scan of **added** lines (debug code, conflict markers; reported `file:line`), forbidden-path guard, missing-test pairing, next-step hints. Generic defaults built in; project policy via config (below). Every mode stamps a `Repo: <toplevel>` header so a wrong-CWD invocation is obvious. In `:PATH` mode a path that is **missing** under the current repo warns `not found … — wrong CWD?` and exits 1, an **untracked** on-disk path warns `untracked (not in git)` — neither is silently reported as `No changes.` (which now means only a clean *tracked* file). Append a trailing **`:full`** in any mode (`git-diff:full`, `git-diff:PATH:full`, `git-diff:staged:full`, `git-diff:branch:BASE:full`) to print the raw `+/-` hunks under a `## Patch` section below the summary — for reading the actual change or writing an honest commit message, without dropping to raw `git diff` |
| `git-merge` | `git-merge:REF` | Merge REF — on conflict surfaces the UU file list, conflict markers, and ours/theirs SHAs |
| `git-conflicts` | `git-conflicts` | List all UU files + every conflict block + abort hint |
| `git-resolve` | `git-resolve:::SIDE:::PATH[,PATH...][:::BLOCKS]` | Pick `ours`/`theirs`/`both` for one file, a comma-separated list, or `all` — stages and prints the continue command. `both` is a union: it strips the conflict markers and keeps both sides (ours then theirs), like git's `merge=union` driver — use it when both branches added different non-overlapping lines. Optional **`BLOCKS`** selector (e.g. `1,3`) resolves only those 1-indexed conflict blocks of a **single** file, numbered exactly as `git-conflicts` lists them; one side per call (mixed sides → run twice). A **partial** resolve leaves the other blocks' markers in place by design, so the file stays conflicted and **unstaged** — the receipt reads `N of M block(s) resolved, file still conflicted`; only when the selector covers every block does the file go clean and get staged. **Self-verifies before staging:** a leftover `<<<<<<<` / `>>>>>>>` is a hard fail (file left unstaged), and each resolved file's receipt carries a warn-only validator digest (`markers: clean \| validate: ok`). **`both` is refused on source files** ([#744](https://github.com/Digital-Process-Tools/claude-supertool/issues/744)) — a union of two versions of code concatenates them, and the result *parses*, so neither the marker gate nor the validator digest can see that the block now runs twice. Refusal is **per file**: `git-resolve:::both:::all` over one `CHANGELOG.md` plus four `.py` resolves the changelog and holds back the four (`⊘ path: source file — 'both' concatenates …`, tally `Resolved: 1 \| Refused: 4`), leaving their markers in place so git itself blocks `rebase --continue`. Two ways through: append **`force`** (`git-resolve:::both:::PATH:::force`) to union anyway — the tally then discloses `Resolved: N (M source file(s) unioned — 'both' concatenates; verify manually)`, because the `validate: ok` beside it is true and useless — or declare the path `merge=union` in `.gitattributes`, which the op honours without a flag. The extension list (`.py .js .ts .php .rb .go .rs .java .sh .sql`, ~32) is a heuristic: it misses an extensionless script and over-fires on a `.sql` file of pure INSERT rows. **`both` is also refused on Markdown when the union would duplicate a heading** ([#839](https://github.com/Digital-Process-Tools/claude-supertool/issues/839)) — the *same* heading line on *both* sides of one hunk, which a line-level union emits twice, reparenting every line between the two copies under the first. On a Keep a Changelog file that turns unreleased entries into shipped ones while the receipt says `markers: clean`. The refusal names the heading it saw; `force` unions anyway and the tally discloses it (`Resolved: N (M file(s) with duplicated heading(s) — verify section structure)`); `merge=union` does **not** bypass this one, because the attribute answers *"union this file"*, not *"this union came out sound"*. The ordinary changelog conflict — two bullets under a shared `### Fixed`, heading outside the hunk — is untouched |
| `git-worktrees` | `git-worktrees[:PATH][:nopr]` | **Is an agent working in this worktree?** Every worktree (or one `PATH`) with branch, path, a merge state in **three** states (`merged` / `not merged` / `merge unknown`, each naming the method — ancestry cannot see a squash merge, see [The merge column](#the-merge-column-has-three-states-and-ancestry-answers-only-one-of-them)), and an occupancy verdict in **three states** — `occupied`, `idle`, `cannot tell` — each naming the evidence it was built from. `cannot tell` is the honest majority answer and is **not** `idle`; see [Occupancy has three states](#occupancy-has-three-states-and-idle-is-the-one-that-must-be-earned). Inspection only: nothing is removed, pruned, unlocked or written |
| `git-commit` | `git-commit:::MESSAGE[:::PATHS...\|:::--all]` | Stage PATHs and commit **only those paths** with MESSAGE — surfaces hook errors, shows HEAD before/after. Omitting PATHS commits the index as it stands, which is the deliberate "commit what I staged by hand" spelling; naming them leaves anything another process staged alone, and the receipt names what stayed staged ([#1228](https://github.com/Digital-Process-Tools/claude-supertool/issues/1228)). Use `MESSAGE=--no-edit` to reuse MERGE_MSG/CHERRY_PICK_HEAD during an in-progress merge or cherry-pick. **PATHS are separated by `:::`** — not by commas and not by spaces; a pathspec git refuses now says so, and offers the `:::` form as a question where a comma-joined token is not a filename git already knows (`a,b.txt` is a legal filename and is never taken apart). **Multi-line body:** use the `@file` route — `git-commit:@-` (stdin) or `git-commit:@msg.toml` — with a `message` field (subject + blank line + body) and an optional `paths` list, instead of dropping to raw `git commit -F` (which skips the trailer below). **Both routes stage** — `paths` is the payload-route equivalent of `:::PATHS`, and the nothing-staged refusal names both, since a caller is usually on the payload route *because* their message will not survive `:`-tokenization. **`MESSAGE=amend` (or `--amend`) is refused** ([#962](https://github.com/Digital-Process-Tools/claude-supertool/issues/962)) — there is no amend route, and committing it produced a commit subject-lined `amend` that then had to be undone; the refusal names `git commit --amend --no-edit` / `-m 'NEW SUBJECT'`, warns that amending a pushed commit rewrites published history *and that this op cannot tell you whether it was pushed*, and offers `SUPERTOOL_ALLOW_LITERAL_AMEND=1` for the genuinely-intended literal subject. The match is exact on the whole message, so `revert the revert` and every other subject merely containing the word is untouched. **Every remedy this op prints names all of the paths it just listed, or none of them** ([#963](https://github.com/Digital-Process-Tools/claude-supertool/issues/963)) — a suggestion carrying the first three of fifteen commits a silent subset when pasted, and past the display cap the line becomes a visible `PATH[:::PATH...]` placeholder carrying both counts (`N paths in all, M shown and N-M not shown`), rather than a partial list that reads as complete. The cap applies to each list separately, so the counts are measured against what was actually printed rather than re-derived from the cap. Suggestions are shell-quoted and the payload array is TOML-escaped, so a path containing `'` or `"` still pastes. Auto-appends a `Co-Authored-By:` trailer when the message lacks one (default `Max <noreply>`), on both the colon-CLI and `@file` routes — configurable via `.supertool.json` (`ops.git-commit.coauthor`) or the `SUPERTOOL_COAUTHOR` env var; disable with an empty value or `none`/`off`/`false`. Omitting PATHS entirely is refused with the **cause** — `ERROR: no PATHS were given — git-commit never stages for you` — not with `nothing staged`, which describes the index rather than the call; `:::--all` is the explicit opt-in that accepts the dirty list the refusal just counted, and its receipt names every path uncapped |
| `git-push` | `git-push[:force-with-lease][:no-verify][:watch][:set-upstream\\|:to-upstream]` | Push the current branch (sets upstream on first push) — remote SHA before/after with commits pushed, ahead/behind vs upstream, and the open MR/PR + pipeline status. For **updating** an already-open MR; use the `mr` op for push+create. **Non-fast-forward** is handled in-op: it fetches, surfaces the **incoming remote commits** (SHA, author, subject) so you can see whose work you'd be rebasing over, then rebases your work onto the remote and re-pushes; on conflict it leaves the rebase **paused**, warns to check the incoming authors before forcing, and points you at `git-conflicts` + the keep-both/cancel/force paths — never auto-forced, never silently rewritten. A **pre-push hook that amends HEAD and pushes** the fixed commit itself (exiting non-zero) is reported as `PUSHED`, not `REJECTED`, since the live remote ref already matches HEAD. A **push that outlasts its budget** (300s, under the op's 420s cap so this script owns the timeout) gets the same treatment rather than a bare timeout failure: `ls-remote` is asked what landed, a remote ref matching HEAD reports `pushed ✓`, and only a remote that genuinely did not move reports `PUSH TIMED OUT ✗` — unverified rather than rejected, with an explicit *fetch before retrying, never force-push on a timeout alone*. The **post-push receipt** carries the next-decision signals (all on calls already made): MR **mergeability** (warns if it now `cannot_be_merged` with target), **stale base** (`N behind origin/<target>`), **uncommitted leftovers** (changes not in this push), the **pipeline id + url**, and a ready `watch:gitlab-mr:<iid>` command. `:force-with-lease` also reports **what it discarded** (author + subject of overwritten remote commits). **Every run ends on a one-line `[result]` verdict** ([#623](https://github.com/Digital-Process-Tools/claude-supertool/issues/623)) — `PUSHED  <branch> -> <remote>/<ref> @ <sha>  (verified)`, or `NOT PUSHED - …` naming which of *already up to date* / *REJECTED* / *REBASE PAUSED* / *UNVERIFIED (timed out)* / *no push attempted (not a repo, detached HEAD)* happened. It is the **last** line, because a verdict that is merely present is one an untracked-file list or a 40-line hook dump scrolls off the end of `| tail -6`; a push that did **not** happen must never render like one that did. The sha in that line is read back off the real remote with `ls-remote`, not just the local remote-tracking ref, so you do not have to `git fetch && git log FETCH_HEAD` to trust it — and when the remote does not answer it says `unverified` and falls back to the tracking sha, labelled, rather than printing a sha it never read. **Uncommitted leftovers are a count, not a listing** — `⚠ N change(s) NOT in this push (uncommitted) — list them: ./supertool 'git-status:full'`; on a working tree full of generated junk the listing was the entire tail of the output. Flags: `:force-with-lease` (safe force — overwrite only if the remote hasn't moved; skips the auto-rebase, and lists discarded commits), `:no-verify` (skip the local pre-push hook, e.g. when a local formatter diverges from CI), `:watch` (spawn a background pipeline poller instead of just recommending the command — it falls back to the running interpreter plus `supertool.py` where the gitignored `./supertool` wrapper is absent, e.g. inside a git worktree, and **names the reason** if it still cannot start rather than degrading to the manual hint ([#642](https://github.com/Digital-Process-Tools/claude-supertool/issues/642))). An **unrecognised flag is refused before anything is pushed** and the op exits `2` naming it and listing what is accepted ([#647](https://github.com/Digital-Process-Tools/claude-supertool/issues/647)) — `git-push:no-verifyy` used to run an ordinary *verified* push while the caller believed the hook had been skipped. A **fetch or rebase on the non-fast-forward recovery path that outlasts its budget** ends on a verdict naming the **worktree state**, not a traceback ([#640](https://github.com/Digital-Process-Tools/claude-supertool/issues/640)): `REBASE IN PROGRESS` with both exits (`git rebase --continue` / `git rebase --abort`), `no rebase started, working tree unchanged`, or an explicit `rebase state UNKNOWN, run git status` when the tool cannot tell — three states, never a guess. The **stale-base check follows the branch's real upstream remote** rather than a hardcoded `origin`, so it still fires on a fork/upstream layout, and prints `⚠ stale-base check skipped` when the target ref does not resolve locally — silence from it means only *the check ran and the base is fresh*. The **force-push discard check has three states too** ([#655](https://github.com/Digital-Process-Tools/claude-supertool/issues/655)): `Force discarded N remote commit(s)` with the list when it ran and found them — and `FORCE-DISCARDED N` on the `[result]` line, so it survives `| tail -3` — **nothing at all** when it ran and found none, and `⚠ DISCARD CHECK DID NOT RUN — <why>` naming the reason plus the command that settles it (`git log <old> --not HEAD`, or `git reflog show <remote>/<branch>`) when it could not. Both quiet states used to be the same empty list as the clean one, so a failed `git log` rendered exactly like a force-push that destroyed nothing — on the one operation here that destroys work irrecoverably, about commits that are usually somebody else's, and `--force-with-lease` does not answer it (a current lease still discards commits you never saw). The pre-push SHA falls back to the one **git itself reports** on the `--porcelain` channel (`+ <old>...<new> (forced update)`), which closes the reachable hole where `@{upstream}` was unset while the remote-tracking ref was current: the lease passed, the push destroyed a colleague's commit, and the check was skipped silently. It never blocks, aborts or re-pushes — the force is your decision; it only refuses to call an unchecked push a clean one. **`(branch created)` is git's claim, not an inference** ([#661](https://github.com/Digital-Process-Tools/claude-supertool/issues/661)): it used to be printed whenever no pre-push SHA had been recorded, and that SHA is missing whenever `@{upstream}` does not resolve — a fact about local config that says nothing about the remote. Since `--force-with-lease` leases against the remote-tracking *ref*, unsetting only `branch.<name>.merge` left the lease passing, the push overwriting an existing branch, and the receipt announcing a **creation** — the least alarming reading available, on the operation that destroys work irrecoverably. The line is now read off git's own `--porcelain` per-ref summary, which separates all four outcomes: `[new branch]` → `(branch created)`, `<old>..<new>` → `Remote <old> → <new> (the branch already existed on the remote)`, `<old>...<new> (forced update)` → the same with `force-updated`, `[up to date]` → `already up to date, ref unchanged`. When git reported nothing readable it says `⚠ Remote now at <sha> — what it pointed at BEFORE this push is UNKNOWN`, names why, points at `git reflog show <remote>/<branch>`, and carries `PRE-PUSH REMOTE STATE UNKNOWN` to the `[result]` line. **The two remaining silent checks in the receipt speak up when they fail** ([#662](https://github.com/Digital-Process-Tools/claude-supertool/issues/662)): the ahead/behind block guarded on `returncode == 0` with no `else`, and an in-sync push legitimately prints nothing there, so a check that could not run was indistinguishable from one that ran and found agreement — it now prints `⚠ vs upstream: UNKNOWN — <command> exited N — <why>`. The uncommitted-leftovers check never read the return code at all, so a failed `git status --porcelain` gave empty stdout, an empty list and therefore silence, which in this receipt means *clean tree* — exactly the run on which "did I forget to commit something?" matters most. It now prints `⚠ UNCOMMITTED-CHANGES CHECK DID NOT RUN — <why>` and says in plain words that it is **not** claiming the tree is clean. Both keep their silence in the working case, so silence stays a positive claim. **The timeout advice names the budget that actually cut** ([#663](https://github.com/Digital-Process-Tools/claude-supertool/issues/663)): it used to say *raise `ops.git-push.timeout` in `.supertool.json`*, which cannot lengthen `_PUSH_TIMEOUT` — a constant in `presets/git/push.py` read from no config — so the advice was followed, nothing changed, and the caller went looking for a slow network instead. Naming a knob that does not govern the thing that cut is a confidently wrong disclosure and worse than silence, because silence does not lie ([#633](https://github.com/Digital-Process-Tools/claude-supertool/issues/633)). The receipt now names `_PUSH_TIMEOUT`, states that the op-level cap alone will not move it and why the two must stay ordered, and points at the lever that works on that path: the push **landed**, so re-running `git-push` prints the full receipt with no configuration change at all. **No post-push check can cost you the receipt** ([#675](https://github.com/Digital-Process-Tools/claude-supertool/issues/675)): every helper the receipt calls after the push has landed runs under a 30s `subprocess.run` timeout, and each one that called it bare turned a `TimeoutExpired` or an `OSError` into a stack trace out of `main()` — for a push whose remote had already moved, so the caller got a traceback and no `[result]` and read it as "the push blew up". All of them now go through one three-state helper: the check answered, the check found something, or it **could not run** and says so by name (`⚠ STALE-BASE CHECK DID NOT RUN — …`, `⚠ UPSTREAM LOOKUP DID NOT RUN — …`, `⚠ vs upstream: UNKNOWN — …`), because a per-helper guard returning `""` stops the traceback and re-introduces the silence it was fixed for. Above that sits a receipt-level guarantee that does not depend on the next call site remembering: whatever raises, the op prints the exception **and** a verdict, and a crash after the push landed reports `PUSHED … (RECEIPT INCOMPLETE — …)` with exit `0`. Two quieter defects on the same path went with it — `_live_remote_sha`, the one helper that *was* guarded, caught `TimeoutExpired` only and let an `OSError` through; and the verdict decided `verified` from `head and live == head`, so a `git rev-parse HEAD` that never answered was reported as `verified, but remote != local HEAD` — a **claim of divergence built out of an absence**, on the one line [#623](https://github.com/Digital-Process-Tools/claude-supertool/issues/623) exists to make you read. **The remote for a first push is resolved, not assumed** ([#656](https://github.com/Digital-Process-Tools/claude-supertool/issues/656)): on a branch with no upstream the op reads `branch.<name>.pushRemote`, then `remote.pushDefault`, then `branch.<name>.remote` — git's own precedence order for a bare `git push`, so it targets whatever `git push` would — then `origin` if such a remote exists, then the only remote if there is exactly one. `git clone -o gitlab` and fork/`upstream`-only layouts used to die on `fatal: 'origin' does not appear to be a git repository`, on the one push where `-u` is the point. With **two or more remotes, none named `origin`, and nothing configured, it refuses**: `ERROR: cannot determine which remote to push <branch> to — this repository has N remotes and none of them is named origin: <names>`, exit `1`, nothing pushed. Guessing wrong on a first push does not fail, it *succeeds* — creating a branch on a remote you never named, plausibly a public one, and pointing every later push at it — so this is a decline rather than a pick (see "Declining instead of guessing" in `docs/validators.md`). To settle it, either `git push -u <remote> HEAD` once, or `git config branch.<name>.remote <remote>` and re-run `git-push`; a repo with **no** remotes at all, and a `git remote` that could not be asked, each say so in their own words rather than sharing one message. **A branch made with `git worktree add -b <branch> <path> origin/master` is not "no upstream" either, and used to be misreported the same way `git-push` had already learned not to** ([#787](https://github.com/Digital-Process-Tools/claude-supertool/issues/787)): `branch.autoSetupMerge` tracks the *start point*, so `@{upstream}` resolves to `origin/master` even though the branch has never been pushed. A bare push there hands the target to `push.default`, which refuses on the name mismatch, and the refusal used to render as `NOT PUSHED - REJECTED  branch -> origin/master` — a target `push.default` picked, not the caller, and a verb implying the remote had acted when the push never reached it. `git-push` now detects `has_upstream and remote_ref != branch` — the exact precondition for that fatal — before invoking `git push` at all, and declines: `NOT PUSHED - no push attempted (<branch>'s upstream is <remote>/<ref>, a different branch — ambiguous target, nothing pushed)`, naming both remedies (`git push -u <remote> HEAD` to push under the branch's own name, or `git push <remote> HEAD:<ref>` to push onto the tracked branch on purpose) |

## What a raw `git` call is refused for

Four of these thirteen ops declare the raw invocation they supersede as `replaces` in `presets/git.json`, and the shipped `PreToolUse` hook refuses that invocation with the op's own description ([#1347](https://github.com/Digital-Process-Tools/claude-supertool/issues/1347), [#1384](https://github.com/Digital-Process-Tools/claude-supertool/issues/1384)). Before this the git ops had no enforcement that shipped with the tool: #1384 measured `git push origin master`, `git commit -m x` and `git -C /tmp/x status` as **allowed, silently**, while two hand-written markdown rules in this repository (`git-push-has-an-op.md`, `git-C-has-cwd.md`) covered the first and the third. Nothing anywhere covered `git commit`.

| Raw command | Refused in favour of |
|---|---|
| `git status` | `git-status` |
| `git commit` | `git-commit:::MESSAGE` |
| `git push` | `git-push` |
| `git push --force-with-lease` | `git-push:force-with-lease` |
| `git push --no-verify` | `git-push:no-verify` |
| `git push -u` / `--set-upstream` | `git-push:set-upstream` |
| `git worktree list` | `git-worktrees` |

### The shapes of a mapped command that are deliberately left alone

`unless_flag` un-claims an entry for a spelling the op does not answer. Each of these runs raw, and that is the design: absence *is* the escape hatch, the opt-out (`raw_command_guard: false`) is repo-wide, and a block naming an op that cannot do the job is a dead end with nothing to route around it.

| Left alone | Why |
|---|---|
| `git push --tags`, `--follow-tags` | there is no tag op; this is the flag-shaped route to pushing a tag on any forge |
| `git push --delete` / `-d`, `--prune`, `--mirror`, `--all` | delete or move refs `git-push` never touches |
| `git push --force` / `-f` | `git-push` offers only `:force-with-lease`, which *refuses* when the remote moved — a different operation |
| `git push --dry-run` / `-n` | a **preview**, and `git-push` has no dry-run route: it pushes. See [A dry run is never answered by the op that does it](#a-dry-run-is-never-answered-by-the-op-that-does-it) |
| `git commit --dry-run` | same: it lists what *would* be committed, and `git-commit` commits |
| `git commit --amend`, `--fixup`, `--squash`, `--allow-empty` | `git-commit` refuses amend by name ([#962](https://github.com/Digital-Process-Tools/claude-supertool/issues/962)) and its own refusal points at raw `git commit --amend` |
| `git status --porcelain`, `-s`, `--short`, `-z` | `git-status` renders prose; `:full` uncaps its lists, it does not emit porcelain |
| `git worktree list --porcelain`, `-z` | same, for the worktree board |

**A short flag in the left column covers its clustered spellings.** `-s` excludes `git status -sb`, which is how most people actually spell it — see [A clustered short flag is read as its letters](#a-clustered-short-flag-is-read-as-its-letters).

### `git push origin <tagname>` is blocked, and that is the one wrong block here

It is discriminated by the **value of a positional**. `origin master` and `origin v0.34.0` are the same argv shape, the same arity and the same token classes; telling them apart means asking the repository whether a ref is a tag, at guard time, before the command runs. No `unless_flag` keyed on tokens can express that, so the bare `git push` entry claims both and the refusal names `git-push`, which does not do tags.

It is disclosed rather than avoided because the alternative is worse in the direction that costs more. Declaring nothing leaves the entire family ungated — the status quo #1384 was filed about — and the routes past this one block are ordinary: `git push --tags` and `--follow-tags` are excluded above and work on any forge, and on GitHub the documented route is `gh api -X POST .../git/refs`. Pinned by `tests/test_git_replaces_1384.py`.

### A dry run is never answered by the op that does it

`git push --dry-run` and `git push -n` were blocked until v0.35.0, and the refusal named `git-push`, **which performs the real push**. Every other way this guard has been wrong costs a *missed block* — a raw read an op could have answered. This costs the opposite, on the one op here that can destroy someone else's commits.

It is worse on a refspec. `git push --dry-run origin feature:refs/heads/other` previews pushing a named branch onto a named ref; a caller who obeys the refusal and runs `git-push` pushes **the current branch to its own upstream** — a ref nobody in that exchange named. `git commit --dry-run` is the same shape and is excluded with it.

`-n` is excluded on `git push` and **not** on `git commit`, because it is a different flag there: `git commit -n` is `--no-verify`, which commits. Pinned by `tests/test_guard_flag_clusters_1425.py`.

### A clustered short flag is read as its letters

`git status -s` was excluded and `git status -sb` was blocked, because the exclusion compared whole tokens. `-sb` is the ordinary spelling of the intent `-s` was excluded for, and the same gap existed for every short flag on every entry — so it is fixed in the matcher rather than enumerated per entry. A single-dash token is now read as a cluster of single-letter flags: an entry excluding `-s` excludes `-sb`, `-bs` and `-sbz`.

**Only exclusions, never the `flag` matcher.** Widening an exclusion makes the guard block *less*, which is the direction it is allowed to be wrong in — a wrong block has no per-command escape short of `raw_command_guard: false`, which disarms every mapping in the repository. Widening the positive matcher would block more, so `git push -uq` is claimed by the bare entry rather than routed to `git-push:set-upstream`.

**A `--` token is never expanded**, or every long flag containing an `f` would un-claim an entry excluding `-f`.

**The cost, which is real:** a short flag carrying a clustered *value* is expanded too, so `git push -ofoo` reads as carrying `-f` and is no longer claimed (while `git push -oci.skip`, spelling no excluded letter, is still claimed — which is the arbitrariness, not a mitigation). Telling that from `-sb` needs per-flag arity the guard does not have. It errs toward allowing, and `git-push` forwards no push options anyway, so that particular block was already a dead end.

### `git -C <path> <subcommand>` reaches none of these mappings

`argv` is matched **token-for-token against the start of a simple command**, and `-C`'s value sits between the command word and the subcommand — so `{"argv": "git status"}` cannot see `git -C /tmp/x status`. The same holds for `git -c key=value`, `--git-dir` and `--work-tree`.

That is a limit of the matcher, not of this preset, and it is deliberately not papered over here: an entry of `{"argv": "git -C"}` is the only spelling `replaces` offers, and it would block `git -C W tag` and `git -C W push origin v1.2.3` alike, neither of which any op answers. The op-side answer to driving another tree is `cwd:PATH` as the first op of the call.

### Nine ops declare nothing

Each is a decision, and each is pinned as an absence in `tests/test_git_replaces_1384.py` so that changing one is a visible edit rather than a silent hole.

| Op | Why its raw form is not claimed |
|---|---|
| `git-diff` | raw `git diff` spans revision ranges, machine formats and pathspecs `git-diff` has no spelling for, and a range carries no flag to exclude it by |
| `git-checkout` | `git checkout <arg>` is two operations sharing one name and the op refuses the pathspec one ([#756](https://github.com/Digital-Process-Tools/claude-supertool/issues/756)) — the same positional-value discrimination as the tag push |
| `git-merge` | `git merge --abort` / `--continue` are what `git-conflicts` itself prints as its hint, and `--no-ff` / `--squash` / `-X` have no op spelling |
| `git-trail` | its raw form is `git log -S`, and a clustered `-Spattern` is not the flag `-S`, so a mapping would fire on one spelling and not the other |
| `git-blame` | the op needs a `LINE`; whole-file `git blame` has no replacement |
| `git-investigate`, `git-diverge`, `git-conflicts`, `git-resolve` | their raw equivalents are flag combinations over `git log` / `git rev-list` / `git diff` whose other uses the op does not answer |

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

### `git-commit` commits the paths you name, and nothing else

Until [#1228](https://github.com/Digital-Process-Tools/claude-supertool/issues/1228) it ran `git add <paths>` and then a **pathspec-less** `git commit`, so anything another process had left staged was committed too. On 2026-08-09 a review agent verified some tests were load-bearing by reverting two files to master and left the revert staged; the next `git-commit` swept it up. The commit **silently un-did 139 lines of a production fix** while the worktree still held the correct file and the tests still passed against the worktree — a commit that removes the fix and keeps the tests green, which is the one shape nothing downstream can catch.

Three things follow, and only the first is the fix:

- **`git-commit:::MSG:::A:::B` commits `A` and `B`.** Foreign staged changes stay staged, exactly where their author left them.
- **The pathless call is unchanged.** `git-commit:::MESSAGE` with no PATHS still commits the index as it stands — that is the spelling that means "commit what I staged by hand", and it is the answer to whether the whole-index behaviour was ever wanted. What changed is that naming paths now means what the signature always said it meant.
- **During a merge or cherry-pick the pathspec stands down.** `git commit -- <path>` is a *partial* commit and git refuses those outright mid-merge (`fatal: cannot do a partial commit during a merge`); a merge commit is whole-index by construction.

And the receipt gained the disclosure that scoping makes necessary. A path staged by somebody else with a clean worktree is invisible to `git status`'s unstaged column, so the `NOT included` block below — which is computed against the **working tree** — cannot see it and never could. This one is computed against the **commit**: whatever is still in the index afterwards is exactly what did not go in.

```
Files committed: 1
⚠ 1 path(s) were already staged and are NOT in this commit:
    presets/watch/transport.py
  git-commit commits the paths you name and leaves the rest of the index alone.
  They are still staged. To commit them too:
    ./supertool 'git-commit:::MESSAGE:::presets/watch/transport.py'
```

The same gap made one refusal wrong in the same direction: with a foreign path staged and a clean worktree, `nothing staged — the working tree is clean` was printed over a non-empty index. That arm now names how many other paths are staged, and points at the pathless call.

### The header echoes the message, and stops once the commit lands

Every op's receipt opens with `--- <the argument you sent> ---`. For
`git-commit` that argument is the commit message, so a long one is printed in
full above a nine-line receipt — the message paid for twice, once to send and
once to read back ([#946](https://github.com/Digital-Process-Tools/claude-supertool/issues/946), [#1235](https://github.com/Digital-Process-Tools/claude-supertool/issues/1235)).

**On a successful commit the header is now a summary**, because `git log -1`
holds the message and the receipt underneath is what proves the commit landed:

```
--- git-commit: "feat(git): rework the receipt" +11 more message lines → 2 path(s): presets/git/commit.py, tests/test_x.py ---
```

**On a refusal it stays verbatim, and that is deliberate.** Nothing was
committed, so the header is the only surviving copy of a message the caller
composed — eliding it there would cause exactly the loss #1235 was filed
about. The summary is gated on the op having succeeded. Short arguments
(under 160 characters) keep their verbatim header either way, as every other
op does.

**A refusal with a multi-line message used to exit 0.** The marker that turns
a `FAIL` receipt into a non-zero process exit was anchored to the line after
the header and could not cross a newline — and the header contains the
message. So `git-commit:::subject` refused and exited 1, while the identical
refusal with one body line added exited 0: a refusal that a hook or a `&&`
chain reads as a success, on exactly the multi-line messages this repo's
conventions ask for. Fixed for every op, not just this one.

### A `:::` inside the message: two readings, both named

`git-commit:::MSG:::PATH` splits on `:::`, so a `:::` *inside* MSG makes the
tail of the message arrive as a PATH. The op already refused that rather than
guessing — but the repair it suggested rebuilt the message by rejoining on a
single `:`, whatever had split it. Pasting the suggestion committed a message
the caller never wrote, under a refusal that was otherwise correct.

The refusal now leads with the `@-` payload route carrying the message
**byte for byte**, and offers the single-colon reading underneath, saying that
it rewrites the separator:

```
  Your message contains ':::', this op's own field separator, so
  no colon form can carry it unchanged. The payload route can —
  it takes the message as bytes:
    ./supertool 'git-commit:@-' <<'EOF'
    message = '''fix(x): thing a::: and more prose'''
    paths = ["a.txt"]
    EOF
  If you meant a single ':' there, this commits 'fix(x): thing a: and more prose'
  — note that the ':::' becomes ':':
    ./supertool 'git-commit:::fix(x): thing a: and more prose:::a.txt'
```

On the **payload** route the same check can fire — a `paths` entry that is not
a path — and there the refusal no longer claims anything was split, because
nothing was: the fields arrived structured. It says which entries are not
paths, leaves the message intact, and declines to guess where the stray text
belongs.

### What `git-commit` did not do

Two renders on this op used to stop one line short of the thing the reader needed, both in the direction where silence reads as completeness.

**The first of them was also actively wrong before #1228**, and that is how the incident above was caught at all: the `NOT included` block named the very two files the commit had just swept in, because it reads the worktree and the worktree still held the correct content. A receipt that contradicted itself, which the author had to `git diff` to resolve.

**A partial commit now names what it left behind** ([#1016](https://github.com/Digital-Process-Tools/claude-supertool/issues/1016)). `git-commit:::MSG:::PATHS` prints a `✓` and `Files committed: N`, which argues nothing was omitted. When modified tracked files are still uncommitted afterwards, they are listed:

```
Files committed: 5
⚠ 2 modified tracked file(s) were NOT included:  (3 untracked, not listed)
    presets/git/commit.py
    tests/test_commit.py
  Intentional? If not: git-commit:::MESSAGE:::presets/git/commit.py:::tests/test_commit.py
```

Named, not counted: "2 not included" costs a second call to find out which, and a reader who has to make that call usually does not. Untracked files are counted only — nearly every worktree has some, and a list of them under every commit is a list nobody reads on the commit that needed it. A run that left nothing behind prints none of this, and a check that could not run says `SKIPPED` rather than nothing.

**Counted is not the same as accounted for** ([#1070](https://github.com/Digital-Process-Tools/claude-supertool/issues/1070)). Two follow-on gaps, same direction:

- the pasteable `Intentional? If not:` line names only the **modified** paths. It is a subset of what the receipt just counted, so it now says so — `The 3 untracked file(s) are NOT in the command above`. A subset is fine; a subset presented as the whole is the defect.
- when the *only* thing left behind was untracked, this whole block was **absent**. A brand-new test file, never committed, under `Files committed: N ✓` and no mention of it anywhere — invisible until CI runs a file that is not in the tree. That case now prints its own line:

```
⚠ 1 untracked file(s) were NOT included (new files are never staged unless you name them).
  Not listed here — see them with: ./supertool 'git-status:full'
```

Still counted, still not listed: the fix is disclosure, not a scratch-file dump.

**A refusal on an unstaged tree now names what is unstaged** ([#1003](https://github.com/Digital-Process-Tools/claude-supertool/issues/1003)). `ERROR: nothing staged.` was correct and unhelpful: the op had just read the working tree and the caller's only remaining move was a raw `git add -A`, i.e. the command this op exists to replace. The refusal itself stays — committing files you did not name is not a default anyone wants — but it now lists the modified tracked and untracked paths separately, and hands back a `git-commit:::MESSAGE:::…` call naming the first few. A genuinely clean tree says so instead, and a `git status` that did not answer says *that*, rather than printing an empty list that reads as clean.

**And it now opens with the cause rather than the symptom** ([#1155](https://github.com/Digital-Process-Tools/claude-supertool/issues/1155)). `nothing staged` is a true statement about the index; what the caller actually did was leave out the argument that fills it. Three callers in one day read that line, concluded the `@payload` route's `paths` key was the missing ingredient, and were wrong — the colon route stages identically, multi-line message included. The first line is now one of four, and which one you get says which mistake was made:

| Situation | Opening line |
| --- | --- |
| no PATHS given, tree dirty | `ERROR: no PATHS were given — git-commit never stages for you, so nothing staged.` |
| PATHS given, none of them dirty | `ERROR: nothing staged — the N path(s) you named held no changes to stage.` |
| tree genuinely clean | `ERROR: nothing staged — the working tree is clean, so there is nothing to commit.` |
| `git status` did not answer | `ERROR: nothing staged, and what is unstaged is UNKNOWN — …` |

It has to be the *first* line and not merely present: supertool's `--- op ---` header replays the whole op string, so on a long commit message everything below line one is under a wall of the caller's own prose. (The echo itself is core, not this preset — see [#946](https://github.com/Digital-Process-Tools/claude-supertool/issues/946).)

**`--all` accepts the list the refusal just counted** ([#1137](https://github.com/Digital-Process-Tools/claude-supertool/issues/1137)). The bare form still refuses, and that is the point — an unrelated file riding into a release commit is exactly what it prevents. But the refusal used to count `24 paths in all` and offer no way to say "those ones", so satisfying it meant dropping to a raw `git status --porcelain` to rebuild the list the op had already computed:

```bash
./supertool 'git-commit:::v0.31.0 release:::--all'
```

Stages every modified-tracked and untracked path (ignored files excluded, as `git status` excludes them) and **lists all of them in the receipt** — uncapped, unlike the 20-path cap on an ordinary commit, because under `--all` the caller never typed the list and the receipt is the only record of what was chosen. `paths = ["--all"]` on the `@payload` route means the same thing.

One long-standing receipt bug had to go with it: `git diff --cached --name-only` runs paths through `core.quotepath`, so `café.txt` printed as `"caf\303\251.txt"` under `Files committed:`. Survivable while the caller had typed the list; not survivable when the listing is the only record. The read is `-z` now, as the refusal path has been since #1003.

Four refusals guard it, all in the same direction — never resolve an ambiguity into a commit:

- `--all` beside a named path is refused. Read wide, the named path was pointless; read narrow, the rest are dropped under a green tick.
- a file literally named `--all` makes the token mean two things, so it is refused and points at `:::./--all` for the file. The payload route is no escape hatch — it reaches the same argument.
- a `git status` that did not answer refuses too, rather than staging the two empty lists it returns.
- `--all` given twice is refused rather than answered with a `git-commit:::MESSAGE:::` carrying an empty trailing pathspec — a remedy that, pasted, lands back on the refusal it was printed under.

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

**Inspection only, on purpose.** Nothing here removes, prunes, unlocks or writes, and the report never prints a `git worktree remove` command. `git worktree remove` refuses while a branch is checked out and after a merge lands, so "which of these can I clean up" is worth *answering* — hence the merge column on each row ([below](#the-merge-column-has-three-states-and-ancestry-answers-only-one-of-them)) — but a destructive suggestion sitting underneath an ambiguous verdict is how an occupied tree gets removed.

**The board is the tool's; the cells in it are not.** A `st-wt/NNN` worktree exists to hold somebody else's branch, so its filenames, its path and its refnames are text this repo did not write — and one of them is printed, because the evidence names the newest write. A filename may contain a newline, and unflattened one file called `a.md␊idle          main                       ~repo  [merged]` rendered **a whole extra worktree row carrying an `idle` verdict** ([#876](https://github.com/Digital-Process-Tools/claude-supertool/issues/876)) — the one verdict that gets a tree deleted. So the render guarantees its own shape: **a row is one line, plus one line per piece of evidence, whatever it is handed.** Branch, path and every filename are flattened, and nothing they contain can reach column 0, add a line, imitate a column gap with a tab, or move the cursor back over a line already printed. Nothing is censored — a control character is shown as itself (`␊`, `␛`) and every other character survives on the line it was given, because the reader is an agent that has to act on the path and an unreadable path is its own failure. The board says so in one line under its heading rather than fencing every row.

**Exit codes carry the same three states.** With a `PATH`: `0` for `idle` and only `idle`, `1` for `occupied`, `2` for `cannot tell`. A caller that tests `== 0` therefore gets the safe reading of an undecided answer. Without a `PATH` it lists every worktree and exits `0` — the tally is in the `[result]` line, which says in words that `cannot tell` is not `idle`.

**A `PATH` that matches more than one worktree exits `2`, not `0`** ([#1282](https://github.com/Digital-Process-Tools/claude-supertool/issues/1282)). The filter is ancestor-or-descendant, so naming a nested tree pulls in the ones above and below it; the board then says nothing about the tree that was asked for. Until this was fixed it printed `3 occupied, 0 idle` and exited `0` at the same time — and `gh-pr-merge`'s `cleanup` arm was reading that code as permission to remove a directory.

**Do not read this exit code as an authorization.** It is a compression of a board with three states into one integer, and the compression is where the meaning goes. `gh-pr-merge` now reads the `[result]` line instead and treats only `0 occupied, 1 idle, 0 cannot tell` as `idle`. Any consumer standing to act irreversibly on the answer should do the same.

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

**It is on by default, and that is a deliberate cost.** Before #941 this op made no network call at all. It now makes two — this one and the merged-PR lookup [below](#the-merge-column-has-three-states-and-ancestry-answers-only-one-of-them) — each on an 8s budget, each independent of the tree count, adding roughly a second and a half in total. Opt-**in** was the alternative and was rejected: the friction being fixed is a join done in the reader's head, and a suffix only helps a reader who already knows the suffix exists, which is not the one reaching for this board at speed. A failed call degrades to a stated `unknown` rather than to a wrong answer, so the downside is bounded. `nopr` — or `SUPERTOOL_WORKTREE_PR=0` — restores the fully offline op, and the `[result]` line counts the rows whose tracker did not answer so a missing answer survives `| tail -1`.

**The exit code is untouched.** It remains a statement about occupancy alone: a tracker that could not be read says nothing about whether a tree is safe to enter, and folding it in would make `git-worktrees:PATH` refuse a free worktree because GitHub was down.

## The merge column has three states, and ancestry answers only one of them

Until [#1229](https://github.com/Digital-Process-Tools/claude-supertool/issues/1229) the `[merged]` tag was decided by `git for-each-ref --merged <base>`. That is an **ancestry** test, and a squash merge writes a commit with no parent link back to the branch — so a fully merged branch is not an ancestor of `master` and never earned the tag. This repository squash-merges every PR.

Measured on the live fleet, 2026-08-10: **24** worktree branches, **8** tagged by ancestry (three of those being new branches sitting at `master`, i.e. trivially ancestors and holding nothing), and **16** with a merged PR on GitHub. Wrong on 16 rows, every one of them in the direction that keeps a stale tree alive.

The worse half was the render. There was no `not merged` token, so a row that failed the ancestry test simply carried **nothing** — and plain absence reads as unmerged work in the op a maintainer uses to decide which trees are safe to reap. Reading those rows the natural way is an argument for re-opening PRs that are already merged.

| token | what it means |
|---|---|
| `merged` | an ancestor of the base, **or** a merged PR whose head is this branch — the evidence line says which |
| `not merged` | both were consulted and neither said yes |
| `merge unknown` | one of them could not answer, and the line names which: offline, `nopr`, the lookup failed, or the merged-PR page hit its cap |

**Ancestry is kept, as a positive only.** A branch that *is* an ancestor of the base has every one of its commits on the base — certain, local, free, and true with no network. It is unsound only as a **negative**, which is precisely the reading removed here. Dropping it altogether was the alternative and was rejected: with `nopr` the merged-PR page is never fetched, and an ancestry-free implementation could then say `merged` about nothing at all. The two signals cannot disagree in the direction that matters, because ancestry is only ever read as a `yes`.

**A second `gh` call, and it has to be a second one.** The tracker column's call is `gh pr list --state open`, and a merged PR is absent from it by construction. The merged page is fetched separately with a single JSON field, on the same 8s budget, and only when the tracker column is on — so `nopr` stays fully offline, and offline the merge column answers `merged` or `merge unknown`, never `not merged`.

**It is scoped by `--search head:…`, not paged, and that is what makes the cap survivable.** The obvious implementation is `gh pr list --state merged --limit N`, and it does not work: merged PRs accumulate forever, so N is a number the repository grows past and never comes back under. Measured 2026-08-10 — 632 merged PRs against a first attempt at `--limit 400`, which made every unmerged branch render `merge unknown` permanently. That is honest and useless: a third state that is always the answer has replaced the wrong answer with no answer. Scoping the query to the branches the board holds bounds the result set by the *question* instead of by the repository's history, and it is faster besides (0.7s against 3.9s for an 800-item page).

**A search that still fills its own limit declines for every branch**, rather than handing back a map that is quietly short — the same rule the tracker column applies to its page, one step stricter, because a partial map is indistinguishable from a complete one at the call site.

**The whole-board `merged-into-base: unknown — <why>` line is gone.** It was the third state applied once, to every row at once; the per-row states supersede it, and two mechanisms for one fact is where a board drifts from what it is measuring. Rows whose merge state is unknown are counted on the `[result]` line instead, so the count survives `| tail -1`.

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

### `ahead N, behind M` after a rebase is not lost work

Straight after a successful `git rebase origin/master`, `git-status` printed `ahead 5, behind 1` and nothing else ([#1028](https://github.com/Digital-Process-Tools/claude-supertool/issues/1028)). That count is arithmetically true and its ordinary meaning is the opposite of what happened: `ahead N, behind M` is the render for two histories that have genuinely diverged, while here nothing was lost and the remote merely holds the pre-rebase originals of commits the branch already carries. Two agents stopped mid-task on separate lanes the same night to work out which of the two they were in.

The count is still printed — suppressing it would trade a confusing render for a quiet one — and it now carries a line that says which kind of divergence it is:

```
Branch: fix/1028 (ahead 5, behind 1)
Diverged: REBASED — every one of those 1 remote commit(s) is patch-equivalent to a commit you already have, so nothing is lost and the remote is stale. Push: ./supertool 'git-push:force-with-lease'
Diverged: 1 of those 4 remote commit(s) are NOT in your history — a genuine divergence. Reconcile (rebase or merge) before pushing; a force push discards them.
Diverged: UNKNOWN whether those 1 remote commit(s) are replays of your own — `git rev-list --count --right-only --cherry-pick HEAD...@{upstream}` did not answer (exit 124: timed out after 5s). This is not saying nothing was lost.
```

The discriminator is `git rev-list --count --right-only --cherry-pick HEAD...@{upstream}`: `--cherry-pick` drops every upstream commit whose patch already exists on this side, so what it counts is exactly the commits the remote has and this branch does not. Zero is a rebase (or any other replay); non-zero is a real reconcile, and the numbers above were measured on a live repository rather than read out of the manual. The extra call is made only when both sides are non-zero, which is the only ambiguous render.

### A printed remedy names the invocation that works where it is printed

`./supertool` is a gitignored symlink. In a linked worktree it does not exist, and in a `claude-supertool` worktree the global `supertool` on `PATH` resolves to a different checkout — so pasting it runs *that* tree's core against *this* tree's presets, the mixed tree [#678](https://github.com/Digital-Process-Tools/claude-supertool/issues/678) discloses after the fact. `git-conflicts` closed its output with `Resolve: ./supertool 'git-resolve:::ours:::PATH'` and `git-push`'s watch advisory said `Run it yourself: ./supertool 'watch:…'`, both at the moment the reader is least likely to second-guess a copy-pasteable command ([#1012](https://github.com/Digital-Process-Tools/claude-supertool/issues/1012)).

Every printed follow-up in these ops now asks what is on disk beside the presets: `./supertool 'op'` where the wrapper exists and is executable, the running interpreter plus `supertool.py 'op'` where only the entry point does, and a stated `(no runnable supertool found in <dir> …)` where neither does — an invented command is a remedy that cannot be run.

The interpreter is `sys.executable`, not the literal `python3` ([#1017](https://github.com/Digital-Process-Tools/claude-supertool/issues/1017)). `python3` is not the launcher on Windows — there it is `py` or `python` — so the hard-coded spelling printed a remedy that did not run on the one platform the authoring machine cannot see. `_watch_argv` had resolved the spawn that way since [#642](https://github.com/Digital-Process-Tools/claude-supertool/issues/642); the printed hint was never covered by that fix.

### When the core itself is conflicted

A rebase that touches this tool's own core leaves `_supertool.py` conflicted, and a file carrying live `<<<<<<<` markers is not valid Python. `supertool.py` is a thin entry point that imports it ([#931](https://github.com/Digital-Process-Tools/claude-supertool/issues/931)), so the failure used to be a `SyntaxError` traceback pointing at a conflict marker — and it took down **every** op, not just `git-conflicts`, for exactly as long as the conflict existed ([#1015](https://github.com/Digital-Process-Tools/claude-supertool/issues/1015)).

The entry point now refuses by name instead: which file, which lines carry markers, that no op can run, that this is not a report of what is conflicted — and a recovery that does not go through the module under conflict. `presets/git/conflicts.py` and `presets/git/resolve.py` import only `_git_common` and `_env`, so they run standalone against the tree you are standing in:

```
python3 /path/to/checkout/presets/git/conflicts.py             # every conflicted file + every block
python3 /path/to/checkout/presets/git/resolve.py ours PATH     # or theirs / both
```

Reaching for the global `supertool` here is the mixed-tree invocation above, and hand-reading the marker range with `git diff --name-only --diff-filter=U` plus `awk` is the hand-rolled resolver `git-conflicts` exists to replace. A syntax error with no markers in it keeps its own diagnosis and is not reported as a merge conflict.

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

### The target branch on that line is the opener's text

`git-push` renders `MR !42 → master` and `⚠ MR conflicts with master` from the request the tracker returned. On any repo that accepts contributions the target — like the source — is a refname chosen by whoever opened the request, and git accepts characters `str.splitlines()` breaks on. Both lines now go through `_untrusted.flat`, so a forged name is one line, in full, unchanged in meaning ([#1038](https://github.com/Digital-Process-Tools/claude-supertool/issues/1038)). `presets/git/status.py` had done this since [#965](https://github.com/Digital-Process-Tools/claude-supertool/issues/965); `push.py` imported `_untrusted` at all for the first time here.

**The more interesting half is why the guard said the file was clean.** `test_forged_branch_line_965.py` walks `presets/git*` for a refname reaching a sink unflattened, keyed on a small set of field names. `presets/git/_git_common.py` *normalises* GitLab's `target_branch` and GitHub's `baseRefName` into one key called `target`, which was not in the set — and two further shapes were invisible to it: `mr['target']` is a subscript where it matched `.get(...)`, and `_open_mr_line` **returns** the f-string its caller prints where it matched `print(...)`. Any one of the three alone was enough for the scan to pass, and report that it had passed, on a tainted render. That is this repo's own defect class — an absence produced by the check, read as an absence in the world — arriving inside the detector built for it.

All three are closed, and the taint tracking is now per function scope rather than per file: `target` is a common local name (`f"{remote}/{ref}"`), and a file-wide dict reported six false findings. A scanner with six false findings is one somebody adds an allowlist to, which is the failure mode this scan exists to avoid, arriving from the other side.

**A third consumer of the same value, one call-hop away, and the scan cannot see it either.** `_post_push_advisories` also hands `target` to `_stale_base_advisory`, which builds `{remote}/{target}` and prints it at column 0 in four places. The value leaves as a *call argument* rather than a print or a return, and the scanner has no interprocedural taint tracking — so it went green over those four lines while the two beside them were being fixed. They are flattened now, but only on the **echo**: `git rev-list` still counts against the real, unflattened ref, because flattening on the way in would change which ref is measured and trade a loud forgery for a quiet wrong answer about how stale the base is (`docs/validators.md`, "the flattening is on the echo only").

So the scan's coverage claim is still not a general one. It sees `print(f"…")` and `return f"…"` of a literal-keyed dict read within one function, in three preset trees. It does not see `%`, `.format()`, string concatenation, `sys.stdout.write`, a value passed to a helper that prints it, or a non-literal key. Keying on the *sink* rather than the source — every f-string interpolating a dict lookup not known-safe — is the version that would have caught this one, and it is not built.

### `git-diff` is the one op here that asks git not to quote, so it is the one that had to fence itself

[#1130](https://github.com/Digital-Process-Tools/claude-supertool/issues/1130) audited every `str.splitlines()` in `presets/git/` — 44 call sites across 12 files — after the same sweep had run over `presets/github` ([#1105](https://github.com/Digital-Process-Tools/claude-supertool/issues/1105)) and `presets/gitlab` ([#1119](https://github.com/Digital-Process-Tools/claude-supertool/issues/1119)). Two were narrowed, both in `git-diff`, and neither was a misattribution: each **suppressed a review gate**.

- `_scan_red_flags` reads `+++ b/` at column 0 to know which file the added lines belong to, and red-flag patterns are extension-scoped. An added line carrying U+2028 followed by `+++ b/notes.txt` retargeted the path, so a `.py`-scoped secret pattern stopped matching every added line after it — the scan switched off by the content it was scanning. This is [#1081](https://github.com/Digital-Process-Tools/claude-supertool/issues/1081)'s `_pr_diff.parse` defect, one op over.
- `_changed_files` splits `--name-status` the same way. A file *named* with a U+2028 before `tests/test_a.py` produced a second, fabricated changed-file record, and `_check_test_pairing` — which asks whether the expected test is in the changed set — stopped warning about a new source file with no test.

**Why these two and not the other 42.** Both readers run `git -c core.quotepath=false`, deliberately, so an accented filename reaches the receipt as itself rather than as octal escapes. That is the right call for a receipt and it is exactly what lets a separator arrive unquoted. Every other porcelain reader in this tree leaves `core.quotePath` at its default, where git octal-quotes every byte above 0x7F in a path it prints — so the separator cannot reach the split at all. The rest are stderr extractions, where taking one line of the split *consumes* the separator and narrowing would be worse (#1105's central finding).

Both are `_untrusted.split_lines` now — LF, CR, CRLF — with the three values that land in a line supertool owns at column 0 flattened: the changed path, the matched content, and the **expected-test name derived from that path**. That third one is the one an independent review of this change caught: `expected` comes from a regex capture over the changed path, so a separator in the filename lands in it too, and flattening `path` beside it without flattening `expected` reintroduced the forged render line on the very line being fixed — a hole that only exists *once* the split is narrowed, which is why nothing before could have found it. Flattened on the **echo only**: `expected` still answers `os.path.exists` and the changed-set membership test unflattened, because flattening on the way in would change which file is looked for (`docs/validators.md`, "the flattening is on the echo only").

`presets/git/resolve.py`'s conflict-marker state machines are the interesting refusal. They split file content and key on `<<<<<<<` at column 0, which looks like the same defect and is not: a forged separator grants nothing a plain newline does not, because a contributor can already put a marker-shaped line at column 0 in their own file. Narrowing there would be motion, not defence.

The 42 remaining sites are each registered with the reason they were left, in `tests/test_preset_git_splitlines_register_1130.py`. A new `str.splitlines()` anywhere under `presets/git/` is a red build until someone writes down which kind it is.

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
