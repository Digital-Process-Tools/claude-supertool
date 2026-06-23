# git

Git investigation and workflow ops. Replaces the 4-6 raw `git` calls you'd normally chain to get a usable picture of a repo's state — `git status`, `git log`, `git diff`, `git blame`, `git log -S`, `git log --follow`. Each op packs the next question's answer into the current call so the agent doesn't need a follow-up turn to decide what to do.

## Requires

`git` installed and a git repository. No auth, no tokens.

## Ops

| Op | Syntax | What it returns |
|----|--------|-----------------|
| `git-status` | `git-status` | Branch, tracking, ahead/behind, last 5 commits, staged/unstaged/untracked files, stashes, open MR/PR link, suggested next step |
| `git-investigate` | `git-investigate:PATH` | File history: recent commits touching the file, uncommitted changes, blame hotspots (most-recently-changed lines) |
| `git-trail` | `git-trail:PATTERN:PATH` | Trace a symbol or string through history via pickaxe search — when it was added, modified, or removed, with contextual diff hunks |
| `git-blame` | `git-blame:PATH:LINE[:N]` | Blame for N lines (default 5) around a specific line number |
| `git-checkout` | `git-checkout:REF` | Switch to branch/tag/SHA — reports tracking state, dirty files, last commits after switch |
| `git-diverge` | `git-diverge:BRANCH[:BASE]` | Branch vs base: ahead/behind counts, commit list, changed files, +/− line totals |
| `git-merge` | `git-merge:REF` | Merge REF — on conflict surfaces the UU file list, conflict markers, and ours/theirs SHAs |
| `git-conflicts` | `git-conflicts` | List all UU files + every conflict block + abort hint |
| `git-resolve` | `git-resolve:::SIDE:::PATH[,PATH...]` | Pick `ours`/`theirs`/`both` for one file, a comma-separated list, or `all` — stages and prints the continue command. `both` is a union: it strips the conflict markers and keeps both sides (ours then theirs), like git's `merge=union` driver — use it when both branches added different non-overlapping lines |
| `git-commit` | `git-commit:::MSG[:::PATH...]` | Stage PATHs (or all staged if omitted) and commit with MSG — surfaces hook errors, shows HEAD before/after. Use `MSG=--no-edit` to reuse MERGE_MSG/CHERRY_PICK_HEAD during an in-progress merge or cherry-pick |
| `git-push` | `git-push[:force-with-lease][:no-verify]` | Push the current branch (sets upstream on first push) — remote SHA before/after with commits pushed, ahead/behind vs upstream, and the open MR/PR + pipeline status. For **updating** an already-open MR; use the `mr` op for push+create. **Non-fast-forward** is handled in-op: it fetches, surfaces the **incoming remote commits** (SHA, author, subject) so you can see whose work you'd be rebasing over, then rebases your work onto the remote and re-pushes; on conflict it leaves the rebase **paused**, warns to check the incoming authors before forcing, and points you at `git-conflicts` + the keep-both/cancel/force paths — never auto-forced, never silently rewritten. A **pre-push hook that amends HEAD and pushes** the fixed commit itself (exiting non-zero) is reported as `PUSHED`, not `REJECTED`, since the live remote ref already matches HEAD. The **post-push receipt** carries the next-decision signals (all on calls already made): MR **mergeability** (warns if it now `cannot_be_merged` with target), **stale base** (`N behind origin/<target>`), **uncommitted leftovers** (changes not in this push), the **pipeline id + url**, and a ready `watch:gitlab-mr:<iid>` command. `:force-with-lease` also reports **what it discarded** (author + subject of overwritten remote commits). Flags: `:force-with-lease` (safe force — overwrite only if the remote hasn't moved; skips the auto-rebase, and lists discarded commits), `:no-verify` (skip the local pre-push hook, e.g. when a local formatter diverges from CI), `:watch` (spawn a background pipeline poller instead of just recommending the command) |

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

**Update an MR that already exists:**
```bash
./supertool 'git-commit:::Fix the thing:::src/app/Thing.py' 'git-push'
```
`git-commit` shows HEAD before/after; `git-push` updates the remote and reports the open MR + the pipeline the push just triggered — no raw `git push` fallback.

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

## Configuration

No project-specific config required. Two environment variables tune `git-investigate` behavior:

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

## Authoring notes

Preset JSON: `presets/git.json`. Helper scripts: `presets/git/` — one Python file per op (`status.py`, `investigate.py`, `trail.py`, etc.). The `{path}` placeholder in `cmd` resolves to `presets/git/` at runtime. Helpers shared across scripts (`_git`, `_first_error_line`, the glab→gh `query_open_mr` lookup) live in `presets/git/_git_common.py`; each script adds its own dir to `sys.path` then imports from it — named `_git_common` (not `_common`) to avoid colliding with `presets/claude-log/_common.py` when both load in the same test process.
