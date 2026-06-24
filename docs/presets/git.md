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
| `git-diff` | `git-diff[:staged\|:branch[:BASE]\|:PATH]` | Review-aware diff (working / `staged` / `branch` merge-base / `PATH`): files grouped by kind + shortstat, red-flag scan of **added** lines (debug code, conflict markers; reported `file:line`), forbidden-path guard, missing-test pairing, next-step hints. Generic defaults built in; project policy via config (below) |
| `git-merge` | `git-merge:REF` | Merge REF — on conflict surfaces the UU file list, conflict markers, and ours/theirs SHAs |
| `git-conflicts` | `git-conflicts` | List all UU files + every conflict block + abort hint |
| `git-resolve` | `git-resolve:::SIDE:::PATH[,PATH...][:::BLOCKS]` | Pick `ours`/`theirs`/`both` for one file, a comma-separated list, or `all` — stages and prints the continue command. `both` is a union: it strips the conflict markers and keeps both sides (ours then theirs), like git's `merge=union` driver — use it when both branches added different non-overlapping lines. Optional **`BLOCKS`** selector (e.g. `1,3`) resolves only those 1-indexed conflict blocks of a **single** file, numbered exactly as `git-conflicts` lists them; one side per call (mixed sides → run twice). A **partial** resolve leaves the other blocks' markers in place by design, so the file stays conflicted and **unstaged** — the receipt reads `N of M block(s) resolved, file still conflicted`; only when the selector covers every block does the file go clean and get staged. **Self-verifies before staging:** a leftover `<<<<<<<` / `>>>>>>>` is a hard fail (file left unstaged), and each resolved file's receipt carries a warn-only validator digest (`markers: clean \| validate: ok`) |
| `git-commit` | `git-commit:::MSG[:::PATH...]` | Stage PATHs (or all staged if omitted) and commit with MSG — surfaces hook errors, shows HEAD before/after. Use `MSG=--no-edit` to reuse MERGE_MSG/CHERRY_PICK_HEAD during an in-progress merge or cherry-pick. Auto-appends a `Co-Authored-By:` trailer when the message lacks one (default `Max <noreply>`) — configurable via `.supertool.json` (`ops.git-commit.coauthor`) or the `SUPERTOOL_COAUTHOR` env var; disable with an empty value or `none`/`off`/`false` |
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
