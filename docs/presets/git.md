# git

Git investigation and workflow ops. Replaces the 4-6 raw `git` calls you'd normally chain to get a usable picture of a repo's state — `git status`, `git log`, `git diff`, `git blame`, `git log -S`, `git log --follow`. Each op packs the next question's answer into the current call so the agent doesn't need a follow-up turn to decide what to do.

## Requires

`git` installed and a git repository. No auth, no tokens.

## Ops

| Op | Syntax | What it returns |
|----|--------|-----------------|
| `git-status` | `git-status[:full\\|:brief]` | Branch, tracking, ahead/behind, last 5 commits, staged/unstaged/untracked files, stashes, open MR/PR link, suggested next step. The `Issue:` line reports only issues a GitHub closing keyword binds to (`Issue: #591`, `Issues: #571, #572`, or a stated `none declared`) — never the first `#N` in the body ([#591](https://github.com/Digital-Process-Tools/claude-supertool/issues/591), see [What `git-status`'s `Issue:` line claims](#what-git-statuss-issue-line-claims)). The default view caps each list (20 staged/unstaged, 10 untracked/branches, 5 stashes) with a `... (N more)` marker — cheap overview. `:full` (alias `:porcelain`) **uncaps every list** for the full untruncated view, e.g. when you need to drive precise staging (excluding a few pre-existing untracked items from a large commit) and can't from a truncated list. `:brief` goes the other way — it drops the local-branch inventory and the last-5-commits log so the working tree and the MR/PR block are near the top ([#1028](https://github.com/Digital-Process-Tools/claude-supertool/issues/1028)); see [`ahead N, behind M` after a rebase](#ahead-n-behind-m-after-a-rebase-is-not-lost-work). A mode that is none of these is named and the default render is labelled as such, rather than silently substituted |
| `git-investigate` | `git-investigate:PATH` | File history: recent commits touching the file, uncommitted changes, blame hotspots (most-recently-changed lines). Every one of those four readers takes a stream git does **not** quote — a commit subject, a diff body, and the blamed file's own lines — so all four are narrowed and every rendered row is `visible()`. The blame call also goes through `_git_verbatim` rather than `_git`, because `_git` runs `subprocess.run(text=True)` and text mode rewrites a lone CR into LF before any splitter sees it — so a bare CR in a source file forged a blame row over plain ASCII, and no choice of splitter closes that ([#1693](https://github.com/Digital-Process-Tools/claude-supertool/issues/1693)) |
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
| `git-push` | `git-push[:force-with-lease][:no-verify][:watch][:budget=SECONDS][:set-upstream\\|:to-upstream]` | Push the current branch (sets upstream on first push) — remote SHA before/after with commits pushed, ahead/behind vs upstream, and the open MR/PR + pipeline status. For **updating** an already-open MR; use the `mr` op for push+create. **Non-fast-forward** is handled in-op: it fetches, surfaces the **incoming remote commits** (SHA, author, subject) so you can see whose work you'd be rebasing over, then rebases your work onto the remote and re-pushes; on conflict it leaves the rebase **paused**, warns to check the incoming authors before forcing, and points you at `git-conflicts` + the keep-both/cancel/force paths — never auto-forced, never silently rewritten. A **pre-push hook that amends HEAD and pushes** the fixed commit itself (exiting non-zero) is reported as `PUSHED`, not `REJECTED`, since the live remote ref already matches HEAD. A **push that outlasts its budget** (300s by default, raisable per call with `:budget=SECONDS` up to 1800s, all of which stays under the op's 1920s cap so this script owns the timeout) gets the same treatment rather than a bare timeout failure: `ls-remote` is asked what landed, a remote ref matching HEAD reports `pushed ✓`, and only a remote that genuinely did not move reports `PUSH TIMED OUT ✗` — unverified rather than rejected, with an explicit *fetch before retrying, never force-push on a timeout alone*. The **post-push receipt** carries the next-decision signals (all on calls already made): MR **mergeability** (warns if it now `cannot_be_merged` with target), **stale base** (`N behind origin/<target>`), **uncommitted leftovers** (changes not in this push), the **pipeline id + url**, and a ready `watch:gitlab-mr:<iid>` command. `:force-with-lease` also reports **what it discarded** (author + subject of overwritten remote commits). **Every run ends on a one-line `[result]` verdict** ([#623](https://github.com/Digital-Process-Tools/claude-supertool/issues/623)) — `PUSHED  <branch> -> <remote>/<ref> @ <sha>  (verified)`, or `NOT PUSHED - …` naming which of *already up to date* / *REJECTED* / *REBASE PAUSED* / *UNVERIFIED (timed out)* / *no push attempted (not a repo, detached HEAD)* happened. It is the **last** line, because a verdict that is merely present is one an untracked-file list or a 40-line hook dump scrolls off the end of `| tail -6`; a push that did **not** happen must never render like one that did. The sha in that line is read back off the real remote with `ls-remote`, not just the local remote-tracking ref, so you do not have to `git fetch && git log FETCH_HEAD` to trust it — and when the remote does not answer it says `unverified` and falls back to the tracking sha, labelled, rather than printing a sha it never read. **Uncommitted leftovers are a count, not a listing** — `⚠ N change(s) NOT in this push (uncommitted) — list them: ./supertool 'git-status:full'`; on a working tree full of generated junk the listing was the entire tail of the output. Flags: `:force-with-lease` (safe force — overwrite only if the remote hasn't moved; skips the auto-rebase, and lists discarded commits), `:no-verify` (skip the local pre-push hook, e.g. when a local formatter diverges from CI), `:budget=SECONDS` (how long the `git push` itself may take, in place of the 300s default — [#1530](https://github.com/Digital-Process-Tools/claude-supertool/issues/1530)), `:watch` (spawn a background pipeline poller instead of just recommending the command — it falls back to the running interpreter plus `supertool.py` where the gitignored `./supertool` wrapper is absent, e.g. inside a git worktree, and **names the reason** if it still cannot start rather than degrading to the manual hint ([#642](https://github.com/Digital-Process-Tools/claude-supertool/issues/642))). An **unrecognised flag is refused before anything is pushed** and the op exits `2` naming it and listing what is accepted ([#647](https://github.com/Digital-Process-Tools/claude-supertool/issues/647)) — `git-push:no-verifyy` used to run an ordinary *verified* push while the caller believed the hook had been skipped. A **fetch or rebase on the non-fast-forward recovery path that outlasts its budget** ends on a verdict naming the **worktree state**, not a traceback ([#640](https://github.com/Digital-Process-Tools/claude-supertool/issues/640)): `REBASE IN PROGRESS` with both exits (`git rebase --continue` / `git rebase --abort`), `no rebase started, working tree unchanged`, or an explicit `rebase state UNKNOWN, run git status` when the tool cannot tell — three states, never a guess. The **stale-base check follows the branch's real upstream remote** rather than a hardcoded `origin`, so it still fires on a fork/upstream layout, and prints `⚠ stale-base check skipped` when the target ref does not resolve locally — silence from it means only *the check ran and the base is fresh*. The **force-push discard check has three states too** ([#655](https://github.com/Digital-Process-Tools/claude-supertool/issues/655)): `Force discarded N remote commit(s)` with the list when it ran and found them — and `FORCE-DISCARDED N` on the `[result]` line, so it survives `| tail -3` — **nothing at all** when it ran and found none, and `⚠ DISCARD CHECK DID NOT RUN — <why>` naming the reason plus the command that settles it (`git log <old> --not HEAD`, or `git reflog show <remote>/<branch>`) when it could not. Both quiet states used to be the same empty list as the clean one, so a failed `git log` rendered exactly like a force-push that destroyed nothing — on the one operation here that destroys work irrecoverably, about commits that are usually somebody else's, and `--force-with-lease` does not answer it (a current lease still discards commits you never saw). The pre-push SHA falls back to the one **git itself reports** on the `--porcelain` channel (`+ <old>...<new> (forced update)`), which closes the reachable hole where `@{upstream}` was unset while the remote-tracking ref was current: the lease passed, the push destroyed a colleague's commit, and the check was skipped silently. It never blocks, aborts or re-pushes — the force is your decision; it only refuses to call an unchecked push a clean one. **`(branch created)` is git's claim, not an inference** ([#661](https://github.com/Digital-Process-Tools/claude-supertool/issues/661)): it used to be printed whenever no pre-push SHA had been recorded, and that SHA is missing whenever `@{upstream}` does not resolve — a fact about local config that says nothing about the remote. Since `--force-with-lease` leases against the remote-tracking *ref*, unsetting only `branch.<name>.merge` left the lease passing, the push overwriting an existing branch, and the receipt announcing a **creation** — the least alarming reading available, on the operation that destroys work irrecoverably. The line is now read off git's own `--porcelain` per-ref summary, which separates all four outcomes: `[new branch]` → `(branch created)`, `<old>..<new>` → `Remote <old> → <new> (the branch already existed on the remote)`, `<old>...<new> (forced update)` → the same with `force-updated`, `[up to date]` → `already up to date, ref unchanged`. When git reported nothing readable it says `⚠ Remote now at <sha> — what it pointed at BEFORE this push is UNKNOWN`, names why, points at `git reflog show <remote>/<branch>`, and carries `PRE-PUSH REMOTE STATE UNKNOWN` to the `[result]` line. **The two remaining silent checks in the receipt speak up when they fail** ([#662](https://github.com/Digital-Process-Tools/claude-supertool/issues/662)): the ahead/behind block guarded on `returncode == 0` with no `else`, and an in-sync push legitimately prints nothing there, so a check that could not run was indistinguishable from one that ran and found agreement — it now prints `⚠ vs upstream: UNKNOWN — <command> exited N — <why>`. The uncommitted-leftovers check never read the return code at all, so a failed `git status --porcelain` gave empty stdout, an empty list and therefore silence, which in this receipt means *clean tree* — exactly the run on which "did I forget to commit something?" matters most. It now prints `⚠ UNCOMMITTED-CHANGES CHECK DID NOT RUN — <why>` and says in plain words that it is **not** claiming the tree is clean. Both keep their silence in the working case, so silence stays a positive claim. **The timeout advice names the budget that actually cut** ([#663](https://github.com/Digital-Process-Tools/claude-supertool/issues/663)): it used to say *raise `ops.git-push.timeout` in `.supertool.json`*, which cannot lengthen `_PUSH_TIMEOUT` — a constant in `presets/git/push.py` read from no config — so the advice was followed, nothing changed, and the caller went looking for a slow network instead. Naming a knob that does not govern the thing that cut is a confidently wrong disclosure and worse than silence, because silence does not lie ([#633](https://github.com/Digital-Process-Tools/claude-supertool/issues/633)). The receipt now names `_PUSH_TIMEOUT`, states that the op-level cap alone will not move it and why the two must stay ordered, and — since [#1530](https://github.com/Digital-Process-Tools/claude-supertool/issues/1530) — names `git-push:budget=SECONDS`, the lever that does move it. Until that flag existed there was none: a `master` push on a repository whose pre-push hook runs the suite could not fit in 300s at all (measured `FAIL (302.86s)` against a suite that took 530.71s), which left `:no-verify` — skipping the very gate — as the only flag that helped. **The budget is the caller's number, not the op's**: `_prepush_hook_state` can see that a hook would run and the op knows the destination ref, but it cannot see what a hook *does*, and "master means the full suite" is this repository's convention rather than a property of git — a self-sized guess that is low is the original defect with extra machinery, and one that is high makes a genuinely hung push wait out somebody else's suite length. An unreadable value, a non-positive one, two disagreeing `budget=` tokens, or one above the ceiling is **refused before anything is pushed, never clamped**, on the same terms as an unknown flag ([#647](https://github.com/Digital-Process-Tools/claude-supertool/issues/647)): a budget quietly rounded down to 300 is a caller who believes they asked for twenty minutes against a clock that cuts in five. On the landed path it still points at the lever that costs nothing: the push **landed**, so re-running `git-push` prints the full receipt with no configuration change at all. **No post-push check can cost you the receipt** ([#675](https://github.com/Digital-Process-Tools/claude-supertool/issues/675)): every helper the receipt calls after the push has landed runs under a 30s `subprocess.run` timeout, and each one that called it bare turned a `TimeoutExpired` or an `OSError` into a stack trace out of `main()` — for a push whose remote had already moved, so the caller got a traceback and no `[result]` and read it as "the push blew up". All of them now go through one three-state helper: the check answered, the check found something, or it **could not run** and says so by name (`⚠ STALE-BASE CHECK DID NOT RUN — …`, `⚠ UPSTREAM LOOKUP DID NOT RUN — …`, `⚠ vs upstream: UNKNOWN — …`), because a per-helper guard returning `""` stops the traceback and re-introduces the silence it was fixed for. Above that sits a receipt-level guarantee that does not depend on the next call site remembering: whatever raises, the op prints the exception **and** a verdict, and a crash after the push landed reports `PUSHED … (RECEIPT INCOMPLETE — …)` with exit `0`. Two quieter defects on the same path went with it — `_live_remote_sha`, the one helper that *was* guarded, caught `TimeoutExpired` only and let an `OSError` through; and the verdict decided `verified` from `head and live == head`, so a `git rev-parse HEAD` that never answered was reported as `verified, but remote != local HEAD` — a **claim of divergence built out of an absence**, on the one line [#623](https://github.com/Digital-Process-Tools/claude-supertool/issues/623) exists to make you read. **The remote for a first push is resolved, not assumed** ([#656](https://github.com/Digital-Process-Tools/claude-supertool/issues/656)): on a branch with no upstream the op reads `branch.<name>.pushRemote`, then `remote.pushDefault`, then `branch.<name>.remote` — git's own precedence order for a bare `git push`, so it targets whatever `git push` would — then `origin` if such a remote exists, then the only remote if there is exactly one. `git clone -o gitlab` and fork/`upstream`-only layouts used to die on `fatal: 'origin' does not appear to be a git repository`, on the one push where `-u` is the point. With **two or more remotes, none named `origin`, and nothing configured, it refuses**: `ERROR: cannot determine which remote to push <branch> to — this repository has N remotes and none of them is named origin: <names>`, exit `1`, nothing pushed. Guessing wrong on a first push does not fail, it *succeeds* — creating a branch on a remote you never named, plausibly a public one, and pointing every later push at it — so this is a decline rather than a pick (see "Declining instead of guessing" in `docs/validators.md`). To settle it, either `git push -u <remote> HEAD` once, or `git config branch.<name>.remote <remote>` and re-run `git-push`; a repo with **no** remotes at all, and a `git remote` that could not be asked, each say so in their own words rather than sharing one message. **A branch made with `git worktree add -b <branch> <path> origin/master` is not "no upstream" either, and used to be misreported the same way `git-push` had already learned not to** ([#787](https://github.com/Digital-Process-Tools/claude-supertool/issues/787)): `branch.autoSetupMerge` tracks the *start point*, so `@{upstream}` resolves to `origin/master` even though the branch has never been pushed. A bare push there hands the target to `push.default`, which refuses on the name mismatch, and the refusal used to render as `NOT PUSHED - REJECTED  branch -> origin/master` — a target `push.default` picked, not the caller, and a verb implying the remote had acted when the push never reached it. `git-push` now detects `has_upstream and remote_ref != branch` — the exact precondition for that fatal — before invoking `git push` at all, and declines: `NOT PUSHED - no push attempted (<branch>'s upstream is <remote>/<ref>, a different branch — ambiguous target, nothing pushed)`, naming both remedies (`git push -u <remote> HEAD` to push under the branch's own name, or `git push <remote> HEAD:<ref>` to push onto the tracked branch on purpose) |

## What a raw `git` call is refused for

Four of these thirteen ops declare the raw invocation they supersede as `replaces` in `presets/git.json`, and the shipped `PreToolUse` hook refuses that invocation with the op's own description ([#1347](https://github.com/Digital-Process-Tools/claude-supertool/issues/1347), [#1384](https://github.com/Digital-Process-Tools/claude-supertool/issues/1384)). Before this the git ops had no enforcement that shipped with the tool: #1384 measured `git push origin master`, `git commit -m x` and `git -C /tmp/x status` as **allowed, silently**, while two hand-written markdown rules in this repository (`git-push-has-an-op.md`, `git-C-has-cwd.md`) covered the first and the third. Nothing anywhere covered `git commit`. `git-push-has-an-op.md` was retired once this mapping landed ([#1376](https://github.com/Digital-Process-Tools/claude-supertool/issues/1376)); `git-C-has-cwd.md` stays, but no longer for the reason it was kept for: [#1421](https://github.com/Digital-Process-Tools/claude-supertool/issues/1421) put `git -C W status` inside the mapping, so that one shape is now refused by the guard as well as by the rule — a double block, filed as [#1438](https://github.com/Digital-Process-Tools/claude-supertool/issues/1438). What the rule still covers alone is `git -C W diff` and `git -C W log`, which stay OK because `git diff` and `git log` are recorded absences rather than mappings.

| Raw command | Refused in favour of |
|---|---|
| `git status` | `git-status` |
| `git commit` | `git-commit:::MESSAGE` |
| `git push` | `git-push` |
| `git push --force-with-lease` | `git-push:force-with-lease` |
| `git push --no-verify` | `git-push:no-verify` |
| `git push -u` / `--set-upstream` | `git-push:set-upstream` |
| `git worktree list` | `git-worktrees` |

### A global option no longer hides the subcommand

`git -C /tmp/x status`, `git -c core.pager=cat status` and `git --git-dir=D status` used to answer `OK: nothing in this command is replaced by an op loaded here` while plain `git status` was blocked — one subcommand, two verdicts, and the second byte-identical to a command nothing maps ([#1421](https://github.com/Digital-Process-Tools/claude-supertool/issues/1421)). The entries above are unchanged; the matcher strips git's own global options before scoring them.

**A terminal option is `OK`, not blocked.** `git --version`, `--html-path`, `--man-path`, `--info-path` and a bare `--exec-path` answer and exit, so the subcommand written after one is never dispatched: `git --version status` prints `git version 2.46.2` and `status` never runs. Scoring it as `git status` was a wrong block on a command git does not execute ([#1437](https://github.com/Digital-Process-Tools/claude-supertool/issues/1437)). `OK` rather than `UNDECIDED` because `UNDECIDED` asserts the guard could not read what would run, and here it could — these tokens have one arity and it ends the command. `--exec-path=P` has the other arity and does run the subcommand, so it is in no list and stays `UNDECIDED`. The same applies to `gh --version pr view 1` and `glab --version mr view 1`, which their own binaries refuse outright.

Two consequences for the table below. **`git -C W tag v1` still runs**, because `tag` is what the walk lands on and nothing maps it — a normaliser with a wrong option table is exactly what would swallow it, which is why the table is explicit rather than heuristic. And **`git -C W push origin v1.2.3` reaches the same verdict as `git push origin v1.2.3`** — `NOT COVERED` since [#1684](https://github.com/Digital-Process-Tools/claude-supertool/issues/1684), `BLOCKED` before it. The consistency is the point either way. Leaving the prefixed spelling clean would have made `-C` a documented bypass for the one op here that can destroy someone else's commits.

An option in none of the three lists — `git --exec-path=P status`, `git --zonk status`, or anything git adds after this was written — is `UNDECIDED`, never a guess. The command runs and the guard says it could not read it.

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

### `git push <remote> <ref>` is not blocked — it is `NOT COVERED`, and says so

This section read "`git push origin <tagname>` is blocked, and that is the one wrong block here" until [#1684](https://github.com/Digital-Process-Tools/claude-supertool/issues/1684), which was filed by someone who hit it cutting a tag. It was worse than a wrong block. **`git-push` pushes the current branch**, so a caller who obeyed that refusal published a ref they never named — a no-op there, a force-with-lease on a stale branch elsewhere — the command reported success, and the tag still did not exist.

The discrimination this section called impossible is not the one that was needed. `origin master` and `origin v0.34.0` genuinely cannot be told apart without asking the repository whether a ref is a tag; **neither has to be**, because both name an explicit refspec and `git-push` names none. Every `git push` entry now declares `"unless_args": 1` — a remote is still claimed, a refspec is not — and the arity is read off the argv with no question put to git.

An un-claimed invocation is **disclosed, not silent**:

```text
NOT COVERED: `git push origin v0.2.0` carries `origin`, `v0.2.0` past the `git push`
that `git-push` replaces, and that op takes none of them: no op covers this form, so
raw `git` is correct here and nothing was blocked. `git-push` is the same invocation
without them, if that is what you meant.
```

**What it costs.** `git push origin master` is no longer refused either — a missed block on a command the op does answer when you happen to be on `master`. That is the direction this guard is allowed to be wrong in: a wrong block has no per-command escape short of `raw_command_guard: false`, which disarms every mapping in the repository. Pinned by `tests/test_guard_positional_arity_1684.py` and `tests/test_git_replaces_1384.py`.

### A dry run is never answered by the op that does it

`git push --dry-run` and `git push -n` were blocked until v0.35.0, and the refusal named `git-push`, **which performs the real push**. Every other way this guard has been wrong costs a *missed block* — a raw read an op could have answered. This costs the opposite, on the one op here that can destroy someone else's commits.

It is worse on a refspec. `git push --dry-run origin feature:refs/heads/other` previews pushing a named branch onto a named ref; a caller who obeys the refusal and runs `git-push` pushes **the current branch to its own upstream** — a ref nobody in that exchange named. `git commit --dry-run` is the same shape and is excluded with it.

`-n` is excluded on `git push` and **not** on `git commit`, because it is a different flag there: `git commit -n` is `--no-verify`, which commits. Pinned by `tests/test_guard_flag_clusters_1427.py`.

### A clustered short flag is read as its letters

`git status -s` was excluded and `git status -sb` was blocked, because the exclusion compared whole tokens. `-sb` is the ordinary spelling of the intent `-s` was excluded for, and the same gap existed for every short flag on every entry — so it is fixed in the matcher rather than enumerated per entry. A single-dash token is now read as a cluster of single-letter flags: an entry excluding `-s` excludes `-sb`, `-bs` and `-sbz`.

**Only exclusions, never the `flag` matcher.** Widening an exclusion makes the guard block *less*, which is the direction it is allowed to be wrong in — a wrong block has no per-command escape short of `raw_command_guard: false`, which disarms every mapping in the repository. Widening the positive matcher would block more, so `git push -uq` is claimed by the bare entry rather than routed to `git-push:set-upstream`.

**A `--` token is never expanded**, or every long flag containing an `f` would un-claim an entry excluding `-f`.

**The cost, which is real:** a short flag carrying a clustered *value* is expanded too, so `git push -ofoo` reads as carrying `-f` and is no longer claimed (while `git push -oci.skip`, spelling no excluded letter, is still claimed — which is the arbitrariness, not a mitigation). Telling that from `-sb` needs per-flag arity the guard does not have. It errs toward allowing, and `git-push` forwards no push options anyway, so that particular block was already a dead end.

### `git -C <path> <subcommand>` reaches these mappings — since #1421, not before

This section said the opposite until #1438, and said it four sections below the paragraph recording the fix. `argv` is matched **token-for-token against the start of a simple command**, and `-C`'s value sits between the command word and the subcommand, so `{"argv": "git status"}` could not see `git -C /tmp/x status` — the answer was the matcher stripping git's own global options before scoring, not an entry here. Same for `git -c key=value`, `--git-dir` and `--work-tree`.

The entries are unchanged, and deliberately: `{"argv": "git -C"}` was the only spelling `replaces` offered, and it would have blocked `git -C W tag` too, which no op answers. What still reaches nothing is `git -C W <sub>` for a subcommand nothing maps, and the flagged spellings each `unless_flag` declines. The op-side answer to driving another tree is `cwd:PATH` as the first op of the call, and `.claude/jit-context/tools/00-manual/git-C-has-cwd.md` says so for the shapes above that the guard does not claim.

### Nine ops declare nothing

Each is a decision, and each is pinned as an absence in `tests/test_git_replaces_1384.py` so that changing one is a visible edit rather than a silent hole.

| Op | Why its raw form is not claimed |
|---|---|
| `git-diff` | raw `git diff` spans revision ranges, machine formats and pathspecs `git-diff` has no spelling for, and a range carries no flag to exclude it by |
| `git-checkout` | `git checkout <arg>` is two operations sharing one name and the op refuses the pathspec one ([#756](https://github.com/Digital-Process-Tools/claude-supertool/issues/756)). An entry is now expressible — `"unless_args": 1` declines `git checkout REF -- PATH`, the file restore, exactly as it declines a refspec push ([#1684](https://github.com/Digital-Process-Tools/claude-supertool/issues/1684)) — but none is declared, so nothing here is blocked |
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
**The `PATH` in those rows is git's own, never yours** ([#1693](https://github.com/Digital-Process-Tools/claude-supertool/issues/1693)). Every `✓` / `⊘` / `✗` / `~` row above interpolates the path raw, with no `_untrusted.flat` — sound because the only strings that can reach one come from `git diff --name-only --diff-filter=U`, which `core.quotePath` octal-quotes, so no byte above 0x7F and no line separator survives. A comma-separated `PATH` argument does **not** widen that: it is a filter over the conflicted set, and anything not already in it is refused before a single row is printed. That is the whole ground, it was written down nowhere for a long time, and it is pinned by a test — relax the refusal and those renders need flattening the same day.

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

When `git add` does fail, whatever the op prints under it is **the route's own
convention, or nothing**
([#1489](https://github.com/Digital-Process-Tools/claude-supertool/issues/1489)).
On the `:::` form a comma-joined pathspec is offered back split, under the
separator rule. On the `@payload` form the rule quoted is `paths`' — a TOML
array, one entry per path — and an entry holding `:::` or `,` is offered back as
an array. A pathspec with no separator in it at all gets neither: git named the
fault, nothing here can name a remedy, and the third state is silence rather
than a rule the caller did not break.

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
[result] NOT PUSHED - REJECTED  feat -> origin/feat - [remote rejected] (protected branch hook declined)
[result] NOT PUSHED - STOPPED BEFORE THE REMOTE  feat -> origin/feat - Connection to github.com closed by remote host.
[result] NOT PUSHED - REBASE PAUSED (conflict in 2 file(s))  feat -> origin/feat - resolve then `git rebase --continue`, or `git rebase --abort`
[result] NOT PUSHED - UNVERIFIED  feat -> origin/feat - push timed out and the remote does not match local HEAD (remote 07a15e1, HEAD b371919)
[result] NOT PUSHED - no push attempted (detached HEAD - checkout a branch first)
[result] NOT PUSHED - no push attempted (fix/x's upstream is origin/master, a different branch — ambiguous target, nothing pushed)
```

**`REJECTED` means the remote refused the ref; a local gate or a dead connection gets its own word** ([#1669](https://github.com/Digital-Process-Tools/claude-supertool/issues/1669)). Both come back to `git-push` as a non-zero exit, and both used to render as `REJECTED` — one line above a `Hint:` that said, correctly, that the push was stopped before it reached the remote and that a rebase would not help. A reader acting on the verdict rebased against a divergence that did not exist. The two are told apart by git's own per-ref status line, the same `--porcelain` channel the non-fast-forward decision reads: a status line means the remote answered, no status line means it never saw the push. The headline (`Status: PUSH STOPPED BEFORE THE REMOTE ✗`), the hint and the verdict word are now all rendered from that one state, so they cannot disagree.

**`First error:` reads git's channel before the hook's** (#1669). A pre-push hook shares stdout with git, and the field used to scan the merged stream for the substring `error` — which matched inside the identifier `OSError`, in a footnote from a **passing** suite's disclosure block, on a push that had actually died in transport with `Connection to github.com closed by remote host.` on the last line. The stream is now partitioned on git's own `To <url>` header (everything above it was written by the hook, nothing below it was) and git's side is asked first. A hook that genuinely blocks a push still surfaces its failing assertion, because git writes only `error: failed to push some refs to '<url>'` on that arm and a line naming nobody is never chosen while another line exists.

**`Status:` on a landed push is derived, not asserted** (#1669). It used to be the constant `Status: pushed ✓`, printed as soon as `git push` exited 0 — so a push with nothing to send sat three lines above `[result] NOT PUSHED - already up to date`, and a consumer keying on `Status:` got a false positive. It now reads the same state the verdict does and says `Status: nothing to push ✓ — the remote ref already matched` when the ref did not move.

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

**The same guard now runs before the push argv, not only before the fetch** ([#1617](https://github.com/Digital-Process-Tools/claude-supertool/issues/1617)). It was on the recovery fetch and on `git-merge` and nowhere else, so an option-shaped remote still reached `git push --porcelain -u <remote> HEAD`. `_resolve_push_remote` will hand one over: its first rung reads `branch.<b>.pushRemote` and `remote.pushDefault` **verbatim**, deliberately, because git accepts a URL in those keys and second-guessing them would make this op target somewhere a bare `git push` would not. On git 2.46.2 / macOS 15 that argv **executes** the payload — git eats `--receive-pack=<cmd>` as its own option, `HEAD` slides into the repository slot, and git starts the receive-pack program for that local path before failing to find a repository there. The `fatal:` that follows is not a mitigation, it just happens afterwards. One remote is enough; no second remote and no steering config are needed. The refusal sits before the argv is built rather than inside each arm, so `remote_name` is also refused on the arm that only reaches `ls-remote` and the post-push advisories with it.

**That sentence was not true when it was written, and now is** ([#1647](https://github.com/Digital-Process-Tools/claude-supertool/issues/1647)). The guard sat 33 lines *below* the first thing that spent the value: `git rev-parse --short <upstream>`, reading the pre-push remote SHA for the header. Not exploitable — `rev-parse` spawns no helper, so an option-shaped upstream dies as an unknown option — but the invariant is what makes it safe to add an arm to this function without re-reading all of it, and a false invariant is worse than an absent one. The guard moved above its first consumer rather than the sentence being weakened to match.

**Platform scope, stated rather than implied.** The execution is proven on POSIX (macOS, Linux, git 2.46.2) — for the fetch sink and, since #1617, for the push sink as well. Whether it also executes on **Windows is not established** — do not read this note as claiming it does. The guard itself is a leading-dash string check with no platform-dependent behaviour, so it closes the sink either way. The test suite carries a positive control that runs the unguarded fetch and proves the sink is live on whatever platform it runs on; the "payload did not run" assertions depend on it, since that assertion passes for free where the payload could never have run. The refusal assertions run everywhere regardless.

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

That dump is bounded — first five lines and last thirty, with the elision counted ([#1448](https://github.com/Digital-Process-Tools/claude-supertool/issues/1448)). Unbounded it printed the child's entire output, and the commonest thing that stops a push is a hook running a test suite: measured against a local `master`, that was an 11,449-item pytest transcript pasted into a receipt. The two ends are the two things you need — the arm the hook announced at the top, and what it refused on at the bottom.

`[remote rejected]` (protected branch, pre-receive hook) is likewise never treated as a divergence — it keeps its own "a rebase will not help" hint.

### The hook says which arm it took, and the receipt now carries it

A pre-push hook is often a *selective* gate. This repo's own runs the full suite when the destination is `master`/`main` and deliberately skips it for a feature branch, announcing which it did every time. None of those lines used to reach you ([#1448](https://github.com/Digital-Process-Tools/claude-supertool/issues/1448)): the op captured the child's streams and printed only its own receipt, so a 7.45s push that skipped the suite and a 226.99s push that ran ~9,600 tests came out the same shape. A selective gate whose selection is invisible is indistinguishable from no gate, and "it pushed fine" then implies a local green nobody earned.

```
Pre-push hook: ran (.githooks/pre-push)
| ── pre-push: feature branch — suite NOT run here ──
|    The PR's checks are the gate, and they run in parallel while you work.
|    Force it locally with: PREPUSH_FULL=1 git push
Status: pushed ✓
```

The `|` lines are the hook's, verbatim. Its **stderr** is relayed too — many hooks write their advice there, and dropping the stream would leave those exactly as silent as before — but under `>` and under a heading that declines the provenance, because git and the remote's own hooks write to stderr as well and nothing marks where one stops. The `To` header is what makes stdout attributable; stderr has no equivalent, so the receipt says `provenance UNKNOWN` rather than guessing.

The op relays rather than summarises: reconstructing what the hook did from its own state lookup and the elapsed time would be the op asserting a fact it never observed, which is what it declines to do everywhere else. Provenance comes from process ordering, not from reading the words — git prints its `To <url>` porcelain header only after the hook has exited, so stdout above that header is the hook's and nothing below it is. If git printed no header, the receipt says the boundary is unknown rather than claiming the stream for the hook.

A long transcript — the `master` path is thousands of pytest lines — keeps its first three and last twelve, because a hook announces its arm on the first line and its outcome on the last. The elision names how many lines it dropped.

The line above the relay has three states, and it is a claim about configuration rather than about what happened:

| Line | Means |
| --- | --- |
| `Pre-push hook: ran (<path>)` | git would run that hook here, and what follows is what it wrote |
| `Pre-push hook: none ran - <why>. Nothing gated this push locally.` | no executable hook, or you passed `no-verify` |
| `Pre-push hook: whether one ran is UNKNOWN - <why>` | git did not answer where the hook lives; not a claim that none ran |

`ran` with nothing after it gets its own sentence — *it printed nothing, so this receipt cannot say which arm it took* — because a silent hook and an absent one otherwise render identically.

One arm carries no relay and says so. A push that outlasts its push budget — `_PUSH_TIMEOUT`, `ops.git-push.budget` or `:budget=SECONDS`, whichever was in force — is killed and its captured output dies with it, so the timeout receipt states that the hook's words were never captured rather than leaving a blank that reads as a hook with nothing to say.

**The rebase-recovery route carries all of it too, and carried none of it until [#1490](https://github.com/Digital-Process-Tools/claude-supertool/issues/1490).** A non-fast-forward hands the push to `_recover_by_rebase`, which runs its **own** `git push` and prints its own receipts — and neither the disclosure above nor the head/tail bound followed it there. So `Status: pushed ✓ (rebased onto remote)` was the one landed-push receipt in this op that said nothing about the hook at all, which is #1448's premise turned back on it: a push that lands after a rebase is precisely a push whose hook has just run. Both of that route's `--- git output ---` dumps are bounded now as well, on the same 5/30 as the straight route, and the rejected-after-rebase arm is where the transcript is largest for exactly the same reason. The `rebase could not start` arm prints no hook line, deliberately: no push of that route's own has run yet, so there is nothing it could say about a hook that would be about the failure it is reporting.

**Everything under `--- git output ---` carries a `> ` prefix, on `git-push` and `git-commit` alike** ([#1569](https://github.com/Digital-Process-Tools/claude-supertool/issues/1569)). The header alone was the containment for a long time, and a header is an *opening* delimiter with no close: under it a pre-commit hook's lines sat at column 0, so one printing `Status: COMMITTED` or `[result] 1 op run, 1 write` wrote lines no reader and no consumer could tell from the tool's own. The prefix says whose line it is; `visible(keep=tab)` per line keeps one line one line. Both are emitted by a single `_git_common.relayed_block`, because the reason `git-commit` had the second without the first was that it had copied the relay out of `push.py` — which a sibling preset cannot import — and dropped half of it.

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

**Every exit names itself in the body, on the line above `[result]`** ([#1496](https://github.com/Digital-Process-Tools/claude-supertool/issues/1496)). `git-worktrees:PATH` on an occupied tree exited `1` and rendered `FAIL` with no line anywhere saying what had failed — because nothing had; the `1` was the occupancy verdict. A caller gating on the status and a caller reading the render disagreed about the same call, and the render was the one with no way to settle it. Now:

```
[exit 1] the occupancy verdict for the one worktree asked about is `occupied` — the op itself did not fail. This integer is the occupancy answer compressed into one and nothing more (0 = idle, 1 = occupied, 2 = cannot tell, or the op could not answer at all)
[result] 1 occupied, 0 idle, 0 cannot tell — …
```

The line sits **above** `[result]`, not below it: that line is what `gh-pr-merge` and every `| tail -1` reader take the tally off. The refusal arms (`PATH` given as an option, `git worktree list` failing, a `PATH` that is no worktree of this repository) each print one too, and the wording separates "nothing was inspected" from "the op failed".

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
| `no open PR` | GitHub answered and holds no open PR for this branch, **and** the publication count was taken and came back 0 — every local commit is on the named ref |
| `N unpushed, no open PR` | the count was taken and N commits here are on no remote |
| `sync not measured, no open PR` | no open PR, and the publication count was **not taken** — the row names why. Not the same fact as the two above, and it used to share their token ([#1525](https://github.com/Digital-Process-Tools/claude-supertool/issues/1525)) |
| `no remote ref` | no remote-tracking ref here: never pushed, **or** the remote branch was deleted after a merge |
| `PR unknown` | the lookup did not run, and this is not a statement about the world |
| `PR n/a` | the worktree has no branch (detached or bare) — nothing to look up |

**`PR unknown` versus `no open PR` is the whole design.** They are the tool's absence and the world's absence, and this repository has paid for that confusion more than for any other single mistake. Rendered as one state they read *this work is unpublished, take the tree* — in the op whose entire job is deciding which tree to take, at exactly the moment (offline, expired token, rate limit) when you are least able to check. So a lookup that did not answer is consulted first, before any local fact, and it says which failure it was.

**A page that hit its cap is `unknown`, not `no open PR`.** One `gh pr list --limit 100` serves every worktree, so N trees cost one call rather than N. But if the page comes back *full*, it is authoritative for the branches it names and establishes nothing about the ones it does not — the partially-answered batch is state 4 wearing state 2's clothes.

**A remote-tracking ref existing is not the same claim as the work being published** ([#1496](https://github.com/Digital-Process-Tools/claude-supertool/issues/1496)). The row used to read `the branch is pushed and no open PR tracks it — the work is published but unproposed` off the *existence* of `refs/remotes/origin/<branch>` alone. Hit live on the default branch: the clone was one commit ahead of `origin/master` and the row said `published`. Read in the direction that matters — somebody deciding whether a tree can be discarded — `published` reads as `safe to remove`. So the count is now taken, with three answers, and the row line itself carries `N unpushed, no open PR` so it survives a scan that never reaches the evidence:

| Measurement | Row says |
|---|---|
| `git rev-list --count <remote-ref>..refs/heads/<branch>` is 0 | `every commit here is also on <ref>` … published but unproposed |
| the count is N > 0 | `N commit(s) here are NOT on <ref>` … the work is **NOT** published: those commits exist only in this clone |
| the count could not be taken, or was not taken | whether every local commit is on the remote it tracks is **UNKNOWN**, with the reason — never `0` |

**The count is taken against the remote the branch actually tracks, and the row names that ref** ([#1525](https://github.com/Digital-Process-Tools/claude-supertool/issues/1525)). It used to prefer `origin` unconditionally whenever two remotes carried the same branch name, so on a fork layout — upstream `fork/X`, an `origin/X` at a different commit — the row counted against a remote it was never about and said so in a sentence naming no remote at all, which the reader could not check. Measured on a two-remote sandbox before the fix: a branch one commit ahead of its `fork` upstream read *in sync with its remote ref … the work is published but unproposed*.

Four inputs, and the row is different in each:

| The branch | Measured against | Row |
|---|---|---|
| tracks `origin/X`, which is here | `refs/remotes/origin/X` | the count, naming the ref and `the remote this branch tracks` |
| tracks `fork/X`, which is here | `refs/remotes/fork/X` | the same, naming `fork` — an `origin/X` at another commit is ignored |
| tracks **nothing** | the same-named ref, `origin` preferred | the count, naming the ref and `picked by name: this branch has no upstream configured` — the commits really are on it, but nothing establishes it as the branch's |
| tracks `fork/X`, which is **not** here (deleted on the remote, or never fetched) | nothing | `sync not measured` and why, naming `fork/X`. An `origin/X` that is here is **not** substituted: `0 commits missing` about a remote the row is not about is the defect, not a fallback |

An upstream naming a *different branch* — `git worktree add -b X … master` can leave X tracking `origin/master` — is not measured against either: that ref resolves and is on a real remote, and it is still not a remote copy of X. Cost is one `git for-each-ref` for the whole board plus one local `rev-list` per *measured* branch, and no extra network call. **Ahead only** — *behind* is a fact about the remote having moved and says nothing about whether this tree's work survives being discarded, which is the question the line is read for.

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
| `git push` (`git-push`) | 300s, or `ops.git-push.budget`, or `:budget=SECONDS` — up to 1800 | The op owns its own timeout so it can verify the remote before reporting; supertool's outer cap must not fire first |
| `git fetch` / `git rebase` on `git-push`'s recovery path | 120s, or what is left of the push budget — and **declined below 30s** | Can land on a worktree git has already paused ([#640](https://github.com/Digital-Process-Tools/claude-supertool/issues/640)); a remainder nothing completes in is a timeout with extra steps ([#1649](https://github.com/Digital-Process-Tools/claude-supertool/issues/1649)) |
| `git commit` (`git-commit`) | 30s | Runs whatever the pre-commit hook chain is |
| `git merge` (`git-merge`) | 30s | Runs merge drivers, potentially over the whole tree |

**An explicit budget wins; the environment sets the default** ([#704](https://github.com/Digital-Process-Tools/claude-supertool/issues/704)). Setting `SUPERTOOL_GIT_TIMEOUT=5` to tighten `git-status` does not cap `git-push`'s 300s and report a push still in flight as failed.

#### `ops.git-push.budget` — the default your repository chooses

`:budget=SECONDS` is per *invocation* — see the deadline section below for what it means within one — and there are repositories where it is the right answer on **every** invocation: a pre-push hook that runs the suite on a push to `master` cannot finish inside 300s, so the flag has to be retyped every session or the push times out having sent nothing. Set the default once instead ([#1631](https://github.com/Digital-Process-Tools/claude-supertool/issues/1631)):

```json
{
  "ops": {
    "git-push": { "budget": 1500 }
  }
}
```

Precedence is **`:budget=SECONDS` > `ops.git-push.budget` > 300**, and 300 is still the answer when neither is set. The key merges over the shipped preset entry key-by-key, so writing `budget` alone keeps the op's `cmd`, `timeout` and everything else; `registry:git-push` renders the merged result with the source of each key.

**It is refused, never clamped, and never silently ignored.** The budget has to stay *strictly* under `ops.git-push.timeout` from the same merged entry — past that cap supertool kills the process, and a killed push cannot ask the remote what landed, which is the verdict this op exists to produce ([#399](https://github.com/Digital-Process-Tools/claude-supertool/issues/399)). A configured value that is not a whole positive number of seconds, is above 1800, is at or above the op timeout, or that could not be checked against the op timeout at all, refuses the push before anything is sent and names both numbers. A push that never happened is recoverable by fixing one line of config; a push under a clock nobody chose is discovered when it cannot be verified.

**Two ceilings, and the smaller one binds — on `:budget=SECONDS` as well as on the key** ([#1659](https://github.com/Digital-Process-Tools/claude-supertool/issues/1659)). `ops.git-push.timeout` is where supertool kills the process; **1800** is the longest this op will make you wait, which raising your own `timeout` does not change. Both apply to both paths, from one implementation. Until #1659 the flag was checked against 1800 alone, so a project writing `"timeout": 60` could still ask `:budget=1700` and have the child killed at 60 — the very outcome the flag's own refusal text cited. That refusal also said the ceiling had to stay under `ops.git-push.timeout` *"in presets/git.json"*, which stopped being where the number lives when #1631 made it yours to set.

A budget the flag overrode is not consulted, so a broken key cannot refuse a push whose clock it does not set. When the config value is the one in force, the receipt says so by name:

```
Push budget: 1500s (ops.git-push.budget — default is 300s)
```

**Why this is not just a bigger default.** The suite behind this repository's own pre-push hook takes 309.86s against the 300s default — ten seconds, which is well inside normal variance. A raised constant would move a repo like this from *always fails* to *sometimes fails*, from the same command; and a repo with no pre-push hook wants a **shorter** budget, because there the only thing a long one buys is a longer wait before an honest failure. The number is per-repo in both directions.

**`git-push`'s budget is a deadline on its pushing, not a per-call timeout** ([#1615](https://github.com/Digital-Process-Tools/claude-supertool/issues/1615)). `:budget=N` means *this op stops pushing within N seconds of starting*, and the clock covers the initial push, the recovery fetch, the rebase and the re-push between them. It used to mean *each `git push` gets N*, which on the non-fast-forward path spent `2N + 240` — so `:budget=1800` asked for 3840s inside an op capped at 1920, and past that cap supertool kills the process, on the one path where the receipt is the only thing that would say the worktree is paused mid-rebase.

The clock opens at the first `git push`, so a run with one push is unchanged. What it costs is on the recovery: a first push that spends most of `N` and is *then* rejected non-fast-forward leaves little or nothing for the rest, and the rest is **declined rather than run short** — `NOT PUSHED - BUDGET SPENT`, naming whether the rebase had already replayed your branch. A `git push` launched on an expired clock is killed before it can verify anything, and on this op the verdict is the whole product. Raise `:budget` and retry; the branch is already rebased, so the retry is a fast-forward. The preamble that picks a remote and the receipt that reads the result stay outside the clock — the receipt deliberately, because an expiring clock past the point of no return must never cost you the answer ([#675](https://github.com/Digital-Process-Tools/claude-supertool/issues/675)).

**"Little" counts as nothing, and the floor is 30s** ([#1649](https://github.com/Digital-Process-Tools/claude-supertool/issues/1649)). The decline used to fire at exactly zero, so a push that spent 295s of 300 launched the recovery fetch on **5s** and reported `fetch TIMED OUT (5s)` — true about the fetch, and misleading about the cause, since nothing was ever going to complete. 30s is what this op already gives `git ls-remote`: one network round-trip to the same remote, ref advertisement only, which a fetch cannot need less than. Measured against github.com on a fast link, 2026-08-14: an already-current `git fetch` took 1.77–2.02s and one that actually transferred 4.33s. Below the floor the receipt says how many seconds were left, what the minimum is, and that a retry gets a fresh budget — a third state, not a clamp.

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

### A copied worktree writes into the repository it was copied from

A linked worktree's `.git` is a **gitfile**: one line of text naming the real git directory. `cp -a` copies that pointer, not the repository — so every git command run in the copy reads and writes the *original* worktree's index, `HEAD` and refs, and nothing in any output used to say so ([#1536](https://github.com/Digital-Process-Tools/claude-supertool/issues/1536)). Observed live: a `git checkout <sha> -- validators/` run inside a copy staged a revert of two production files into a worktree nobody was watching, and it came within one `git-commit` receipt of riding into the commit that claimed to fix them.

It is decidable exactly, locally, with no filesystem scan and no extra spawn: `.git/worktrees/<name>/gitdir` holds the path of the `.git` file git registered for that worktree. If this directory's own `.git` is not that file, this directory is not the registered one. `git-status`, `git-worktrees`, `git-commit` and `git-push` all say so, `git-status` and `git-worktrees` before any other line, because none of the numbers below it is a fact about the directory the reader is standing in:

```
⚠ COPIED WORKTREE — this directory is not the one git registered for its git directory; the index, HEAD and refs reached from here belong to /Users/x/wt (#1536)
```

The `Repo:` line on `git-commit` and `git-push` exists to say where a write landed ([#692](https://github.com/Digital-Process-Tools/claude-supertool/issues/692)), and in a copy it named the copy — the one directory the write does **not** reach. It now carries the same disclosure underneath.

**Both paths in that banner are flattened, and the separator is disclosed rather than removed** ([#1557](https://github.com/Digital-Process-Tools/claude-supertool/issues/1557)). They come off disk — `gitdir` holds whatever wrote it, and git writes the path handed to `git worktree add` into it verbatim — so a worktree directory whose *name* contains a line separator used to put its own text at column 0 of the render, where it was indistinguishable from a section the op wrote. That needs no attacker: a newline in a directory name is enough. A separator now shows as `␊`, or `[U+000A]` on a console that cannot carry the control pictures:

```
⚠ COPIED WORKTREE — … the index, HEAD and refs reached from here belong to /Users/x/wt␊### Staged (0) (#1536)
```

Disclosed rather than elided because the value is a **path**: replacing the separator with a space would name a directory that is not on disk, and the point of the line is that the reader can go and look at the tree their writes are reaching. The flattening is done by `foreign_worktree()` itself, so it covers the four places that render those paths — this banner, the sentence under it here and in `git-worktrees`, and the `Repo:` line — rather than the one that was reported.

Nothing here scans the disk for copies: a copy can be anywhere, and an op that goes looking would answer "none found" for a search it could not complete. What is checked is the one directory the call was made from, which is the one that can be settled.

**Never `cp` a worktree.** `git worktree add` is the operation.

### Staged content that no file in this tree has

`git-status` cannot tell who staged something. What it can see is that the index differs from `HEAD` while the file on disk still matches `HEAD` — staged content that no file here has, which is exactly the shape a stray `git checkout <sha> -- <path>` leaves, and equally the shape of a stage you later undid by hand. So the render names what it could not determine rather than listing it under `Staged` like ordinary work, and it never suppresses the list ([#1536](https://github.com/Digital-Process-Tools/claude-supertool/issues/1536)):

```
### Staged (1)
  MM validators/fence.py
⚠ STAGED CONTENT NOT IN THIS TREE (1) — the index differs from HEAD while the file on disk matches it, so committing these would write content no file here has. …
    MM validators/fence.py
```

Three states, as everywhere else here: the discriminator is one `git diff --name-only -z HEAD`, asked only when something is staged, and a `git diff` that did not answer prints `⚠ Staged provenance UNKNOWN — …` rather than the silence that reads as a clean answer.

`-z` is load-bearing, not tidiness. **The two readers do not print the same path**, measured on a real repository:

```
git status --porcelain=v1     M  "with space.txt"     M  "uni \303\251.txt"
git diff --name-only HEAD        with space.txt          "uni \303\251.txt"
```

porcelain quotes a space because its own format is space-separated; `--name-only` does not, and `core.quotePath` has no bearing on that half. Comparing the printed forms put every staged file with a space in its name under the marker above. `-z` hands the raw path back from the diff side, and porcelain's quoting is undone on the other, so the comparison is between the two names git actually holds.

One state is silence rather than a third state, deliberately: a repository with **no commits yet** has no HEAD for the index to be a revert of, so the question is meaningless rather than unanswered, and `git init && git add .` gains nothing to skim past. It is established rather than inferred — `git rev-parse --verify --quiet HEAD` answers "no such ref" as exit 1 with an empty stderr, spawned only where the diff has already failed. A probe that timed out, or failed with something to say, has not established anything and the `UNKNOWN` line prints.

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

### A child's stream is somebody else's text, and the flatten belongs at the seam

[#1470](https://github.com/Digital-Process-Tools/claude-supertool/issues/1470) closed the `git-push` relay and [#1475](https://github.com/Digital-Process-Tools/claude-supertool/issues/1475) named seven sites it had not. Fixing those seven one at a time is the same defect re-filed once per call site, so the flatten went where every one of them already passes: `_git_common._first_error_line` now returns `_untrusted.flat(...)`. `git-commit`, `git-push` and whatever is written next are covered at once, and `flat()` is idempotent, so `push.py` — which flattens the return again at its own render — pays a no-op rather than a second substitution.

The same function also parses with `_untrusted.split_lines` instead of `str.splitlines()`. It scans a child's stream for the salient line, which is a **line-oriented parse**, and `str.splitlines()` folds on ten separators no git stream defines ([#1081](https://github.com/Digital-Process-Tools/claude-supertool/issues/1081)). A hook that put a U+2028 in a line was choosing which line the scan returned — and the tail it hid was dropped from the receipt entirely rather than disclosed, which is the worse half.

`git-commit`'s refused-commit render moved into `_failure_receipt`, a function returning lines rather than five `print`s inside `main`, so it can be driven with a hostile stream and no repository. It relays the hook transcript the way `git-push` relays its own: `split_lines`, then `visible(keep=tab)` per line. Tabs survive — a transcript is not parsed by column — and separators are shown rather than dropped.

**The detector is a ratchet, and that is a decision rather than a shortcut.** An AST scan for "a child stream reaches a render with no marker between" reports **144 sites in 32 files** across `presets/git`, `presets/github` and `presets/gitlab`. Not all are defects: `push._local_head` returns `r.stdout.strip()` and that is a SHA. So `tests/test_forged_child_stream_line_1475.py` records a count per file that may only fall.

**What "a render" means is written down, because the scan pattern-matches shapes rather than deriving them.** `SINK_SHAPES` names the five it recognises — `print(...)`, `return <expr>`, `<obj>.write(...)`, `<obj>.append(...)`, `<obj>.extend(...)` — and a test probes each one, so a shape listed but no longer matched fails rather than reading as a shape with no instances. Until [#1626](https://github.com/Digital-Process-Tools/claude-supertool/issues/1626) the set was `print` and `return` alone, and it cost exactly what an undisclosed population costs: of the fifteen `presets/github/` relays [#1606](https://github.com/Digital-Process-Tools/claude-supertool/issues/1606) fixed, four went through `sys.stderr.write` and scored 0 both before and after the fix.

**The count above is a bounded claim, and the bound is published too.** `UNRESOLVED` counts child-stream values that flow into a call the scan does not model at all — a helper, a formatter, a `join`. It is **not** a defect count; it is the size of the population the census says nothing about, and it is asserted exact in both directions so that narrowing the scan lowers it and fails visibly ([#1570](https://github.com/Digital-Process-Tools/claude-supertool/issues/1570)). Both published totals are checked against the sweep by `test_the_published_total_is_the_measured_one`, which is why this paragraph cannot go stale the way the previous number did. A new relay raises a number and fails; a fixed one lowers it and fails with the message telling you to write the smaller number down. There is nothing to add a site to, which is what separates it from the allowlist [#1475](https://github.com/Digital-Process-Tools/claude-supertool/issues/1475) refused to ship — and what it cannot catch is stated in the test itself: a relay added to `push.py` in the same commit that fixes another one nets out.

### `git-diff` is the one op here that asks git not to quote, so it is the one that had to fence itself

[#1130](https://github.com/Digital-Process-Tools/claude-supertool/issues/1130) audited every `str.splitlines()` in `presets/git/` — 44 call sites across 12 files — after the same sweep had run over `presets/github` ([#1105](https://github.com/Digital-Process-Tools/claude-supertool/issues/1105)) and `presets/gitlab` ([#1119](https://github.com/Digital-Process-Tools/claude-supertool/issues/1119)). Two were narrowed, both in `git-diff`, and neither was a misattribution: each **suppressed a review gate**.

- `_scan_red_flags` reads `+++ b/` at column 0 to know which file the added lines belong to, and red-flag patterns are extension-scoped. An added line carrying U+2028 followed by `+++ b/notes.txt` retargeted the path, so a `.py`-scoped secret pattern stopped matching every added line after it — the scan switched off by the content it was scanning. This is [#1081](https://github.com/Digital-Process-Tools/claude-supertool/issues/1081)'s `_pr_diff.parse` defect, one op over.
- `_changed_files` splits `--name-status` the same way. A file *named* with a U+2028 before `tests/test_a.py` produced a second, fabricated changed-file record, and `_check_test_pairing` — which asks whether the expected test is in the changed set — stopped warning about a new source file with no test.

**Why these two and not the rest — and the answer this doc gave for a year was too wide.** Both readers run `git -c core.quotepath=false`, deliberately, so an accented filename reaches the receipt as itself rather than as octal escapes. That is the right call for a receipt and it is exactly what lets a separator arrive unquoted. What was written next — that every other reader in this tree leaves `core.quotePath` at its default and so cannot see a separator at all — is true only of the readers that read a **path**. [#1654](https://github.com/Digital-Process-Tools/claude-supertool/issues/1654) measured it on git 2.46.2: `ls-files` octal-quotes a non-ASCII name, and `log --format=%s`, `for-each-ref`, `branch -vv`, `show` content and `blame --line-porcelain` all hand back a U+2028 raw. stderr is never a pathname, so quoting was never in it. The rule covered **3 of the 27 register entries** and was quoted at all 27.

The rest are not defects, and most of them are safe on grounds just as good: a forged row can only refuse (`_list_conflicts`), or is fail-closed (`_union_attr_paths`), or inflates a count in its loud direction, or its writer is the local operator (`_remotes_could_host_a_request`). Each entry now opens with the ground it rests on — `QUOTED PATH`, `NOT QUOTED, harmless`, `NOT QUOTED, open` — and the register refuses one that names none.

Two entries turned out to rest on nothing and were narrowed by #1654. `merge.py::_fresh_merge_ref` took the **last** line of a failed `git fetch`'s stderr, a stream a remote writes `remote:` lines onto, and put it unflattened into the `WARN: fetch … failed` the caller acts on: a U+2028 ahead of a reassuring tail meant `str.splitlines()` handed back the tail alone and dropped git's own `fatal:` off the front. `worktrees.py::remote_branch_names` had both halves at once — its stderr decline took the **first** line of an unmarked stream, and its `for-each-ref` read let one published ref become two records, because `check-ref-format` exits 0 on a refname whose middle component ends in U+2028 followed by a second `refs/remotes/origin/…`. That is the pushed/unpushed forgery the register itself listed as open.

The argument that kept four of these — that taking one line of the split *consumes* the separator, so narrowing would be worse — is #1105's central finding and half of one. It was retired for `presets/github/` by [#1648](https://github.com/Digital-Process-Tools/claude-supertool/issues/1648) and is retired here: consuming the separator means discarding everything on the other side of it, so the writer of the stderr still chooses which segment becomes the message and the rest is dropped rather than disclosed. What answers both halves is `_untrusted.split_lines` for the boundary *and* `_untrusted.flat` on the segment it picks.

Both are `_untrusted.split_lines` now — LF, CR, CRLF — with the three values that land in a line supertool owns at column 0 flattened: the changed path, the matched content, and the **expected-test name derived from that path**. That third one is the one an independent review of this change caught: `expected` comes from a regex capture over the changed path, so a separator in the filename lands in it too, and flattening `path` beside it without flattening `expected` reintroduced the forged render line on the very line being fixed — a hole that only exists *once* the split is narrowed, which is why nothing before could have found it. Flattened on the **echo only**: `expected` still answers `os.path.exists` and the changed-set membership test unflattened, because flattening on the way in would change which file is looked for (`docs/validators.md`, "the flattening is on the echo only").

`presets/git/resolve.py`'s conflict-marker state machines are the interesting refusal. They split file content and key on `<<<<<<<` at column 0, which looks like the same defect and is not: a forged separator grants nothing a plain newline does not, because a contributor can already put a marker-shaped line at column 0 in their own file. Narrowing there would be motion, not defence.

Its *relays* were a different question and were real ([#1638](https://github.com/Digital-Process-Tools/claude-supertool/issues/1638)): the failed `git checkout --ours/--theirs`, the staging `git add`, and the same `git add` on the block-selector path each put a child's `stderr or stdout` into the `✗ PATH: REASON` row unmarked. All three are `_untrusted.flat`-ed now, the treatment fifteen `presets/github/` sites got in #1622. The `cannot read: {e}` reason beside them is deliberately not: `str(OSError)` reprs the filename, so a separator in a conflicted path arrives as its escape and cannot open a line — flattened in a first cut of that change, then un-flattened, and pinned by a test rather than by a comment.

**A fourth question, which the three grounds cannot ask** ([#1681](https://github.com/Digital-Process-Tools/claude-supertool/issues/1681)). Eleven sites in six functions did not *select* a line — they rendered **every** line, counted: `git-checkout`'s last-3-commits list, `git-diverge`'s commit list, `git-push`'s force-discard and incoming lists, `git-status`'s other-branches, last-5 and stash sections, and all three of `git-trail`'s. Each was correctly registered `NOT QUOTED, harmless` on the ground that a forged split can only inflate a count in its loud direction. That answer is right about the row and wrong about the site, because at an every-line render the count *is* the product: `Force discarded N remote commit(s)` is the only statement `git-push` makes about what a force-push destroyed.

`_untrusted.split_lines` alone is the wrong repair here, and that is why this was filed separately rather than folded into #1654. Consuming the separator is right where a line is being selected; where all of them are rendered, narrowing the split leaves the separator un-consumed and live inside a row the tool presents as its own — an inflated count traded for a cursor command. All eleven take `split_lines` **and** `visible()` per rendered line, with `keep` set to the TAB only where the line is parsed on a TAB field (`for-each-ref`) or is indented source (a `git show` hunk).

Two of the eleven were more than a forged row:

- `git-trail`'s pickaxe render hands each line's first token to `git show` as argv, and `git show --output=<file>` writes that file (git 2.46.2). A subject ending `<U+2028>--output=/path` became a line whose first token was that option — `splices`, not `forges`, reached from a commit message.
- `git-status`'s other-branches section *lost* a branch rather than gaining one. A refname carries U+2028 (`check-ref-format` accepts it), the head fragment has no TAB so the `"ahead" in track` test drops it, and the surviving row rendered under a truncated name no branch here has. The register entry's own reasoning — "a fragment has no track field and is dropped" — was the description of the bug.

The 27 remaining sites across 24 enclosing functions in 9 files are each registered with the ground they rest on, in `tests/test_preset_git_splitlines_register_1130.py`. A new `str.splitlines()` anywhere under `presets/git/` is a red build until someone writes down which kind it is — and, since #1654, until they name one of the three grounds rather than inheriting a rule that answered for three entries out of twenty-seven.

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
