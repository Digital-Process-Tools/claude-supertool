# gitlab

GitLab ops via the `glab` CLI. Replaces the 3-5 separate `glab` calls needed to review an MR (branch, pipeline, approvals, diff, comments) and debug a failed job. Each op returns a full dashboard so the agent doesn't need a follow-up turn to locate the failure.

## Requires

[`glab` CLI](https://gitlab.com/gitlab-org/cli) installed and authenticated.

## Ops

| Op | Syntax | What it returns |
|----|--------|-----------------|
| `gl-issue` | `gl-issue:NUMBER[:full]` | Issue metadata, description, comments (truncated by default), related MRs. `:full` disables truncation |
| `gl-mr` | `gl-mr:NUMBER_OR_BRANCH[:status\|:full]` | MR dashboard: branch, pipeline, reviewer/approval state, linked issue, diff stat, per-file name-status (`A`/`D`/`R`/`M`) list, comments. The file list is the high-signal "what got removed?" scan; capped at 50 files by default with a `… +N more` marker. `:status` returns slim merge-state plus the `source -> target` branch line only; `:full` uncaps the file list (paginating up to 500) and the comments. The `Conflicts:` line only says `YES` on positive evidence of an actual diff conflict; an MR blocked because its source branch has no commits prints `Conflicts: NO — cannot merge: source branch has no commits, so there is nothing to merge` instead (same `conflict`/`empty` distinction as `gl-mrs`, #494). A real conflict adds a `## Conflicts` section: the conflicting paths, then a per-file hunk preview (capped at 40 lines each) and the resolve commands. A conflicted **binary** file (image, PDF, font) is still listed and still gets its `###` heading, but the hunks are replaced by `(binary file — conflict hunks not shown; resolve by picking a version)` — `git merge-tree` streams raw blob content, and rendering 40 lines of mojibake helps nobody (#498). When the preview cannot be computed at all — the `git merge-tree` that produces the hunks timed out, exited non-zero, or could not be run — the section says so and names the cause, instead of rendering as a conflict that happens to have no hunks (#507); see [The conflicts section](#the-conflicts-section) |
| `gl-mrs` | `gl-mrs[:filters,flags]` | MR triage board, sorted failing-first then stalest. Per MR (enriched in parallel): pipeline status (a failure shows the failed **job name** = the failure class), approval state, age, diff size, watch-state cross-reference, and `conflict`/`empty`/`draft`/`threads` flags — plus an actionable footer. "Failing" here means red, not the literal string `failed`: `canceled`, and any pipeline state GitLab adds later, sort first and are matched by the `failed` flag and the footer count. `skipped`/`neutral`/`manual` are the only non-success states treated as benign. Filters (comma-sep): `author`/`reviewer`/`assignee`/`label`/`milestone`/`state`/`per`. Flags: `nopipe` (skip enrichment), `iids` (bare id list), `failed` (only failing) |
| `gl-pipeline` | `gl-pipeline:NUMBER[:active\|:failed]` | Pipeline job list grouped by stage with pass/fail status and failed job IDs. The default board collapses the `manual`/`created`/`skipped` bulk to a one-line count so the running/done/failed jobs aren't buried. `:active` shows only running/pending jobs ("what's still going"); `:failed` shows only failed jobs plus their job IDs/URLs ("what broke") |
| `gl-job` | `gl-job:NUMBER[:raw[:-N\|:START[:END]]\|:grep:PATTERN]` | Job failure detail: MR context + error pattern search + log tail. `:raw` dumps the full trace; `:raw:START:END` slices lines (1-indexed, inclusive); `:raw:-N` returns the **last N lines**; a START past the end of the log returns the tail of the width requested and says so, rather than declining (see [Reading a range](#reading-a-range)); `:grep:PATTERN` runs an ad-hoc regex over the trace (literal fallback on bad regex, ±context, names the pattern + tail on no-match — never silent-empty). Cause markers are always matched on top of the configured patterns, and a *failed* job that matches nothing is reported as unclassified with a log tail rather than as "no errors" |

**The local-branch check on `gl-job`** prints under the `Branch:` line and always states one of two things — `✓` when your checkout matches, `⚠ MISMATCH` when it does not. On the read-only sub-ops (`:raw`, `:fail`/`:errors`, `:grep`) the mismatch is stated **without** the `git-checkout` suggestion ([#531](https://github.com/Digital-Process-Tools/claude-supertool/issues/531)): those are what a monitoring session runs, moving HEAD is the one action such a session must never take (fixes go to a worktree so the checkout stays put), and the hint fired hundreds of times a session always pointing the wrong way. Only the imperative is withheld, never the state — the line prints either way, so a missing checkout command can never be misread as "you are on the right branch". The bare `gl-job:NUMBER` view keeps the suggestion: you open a failure in order to fix it, which is exactly when it earns its line. `gl-mr:NUMBER:status` prints no branch check at all and never did.

**Casing:** `gl-mr`'s full dashboard prints `Title Case:` labels throughout (`State:`, `Pipeline:`, `Merge status:`, `Merge commit:`); `:status` prints `lowercase:` labels throughout (`state:`, `pipeline:`, `merge_status:`, `merge_commit:`) as part of its terser, slim-dashboard format. Each mode is internally consistent but the two never match each other, so grep the casing for the mode you're reading, not both — the same split as `gh-pr`, see `github.md`.

## Common workflows

**Review an MR before merging:**
```bash
./supertool 'gl-mr:42'
```
Returns approval state, pipeline status, diff stat, and all comments in one call.

### The conflicts section

A conflicted MR gets a `## Conflicts` block: the conflicting paths, a per-file hunk preview, and the resolve commands. Two `git merge-tree` calls feed it, and the distinction matters when reading the output:

| Call | Produces | Carries blob content |
|------|----------|----------------------|
| `git merge-tree --write-tree --name-only` | the conflicted **file list** | no |
| `git merge-tree BASE TARGET SOURCE` | the per-file **hunks** | yes |

Only the second can be slow or blow up, because only it streams the conflicting blobs — one conflicted PNG is 230KB+ of stdout ([#498](https://github.com/Digital-Process-Tools/claude-supertool/issues/498)). So a hunk failure never means the file list is wrong.

The section therefore distinguishes three outcomes, which used to render identically ([#507](https://github.com/Digital-Process-Tools/claude-supertool/issues/507)):

**Hunks computed and present** — a `###` heading per file with the `<<<<<<<` markers, capped at 40 lines each. No note.

**Hunks computed, and there genuinely are none** — add/add, delete/modify and rename conflicts have no inline hunks to show:

```
  No hunk preview for 1 file: src/foo.py
  — git merge-tree returned no conflict content there (add/add,
  delete/modify and rename conflicts have no inline hunks).
```

**Hunks not computed** — timeout, non-zero exit, or git could not be run at all. The cause is named, and the note is explicit that the file list above survives it:

```
  Hunk preview unavailable: git merge-tree timed out after 20s.
  The conflicted file list above is still accurate — it comes from a
  separate `git merge-tree --write-tree --name-only` call that carries
  no blob content, so it cannot fail this way.
  To see the hunks, run the merge locally with the commands below.
```

The resolve commands print in all three cases; they never needed the hunks.

**The hunk timeout scales with the conflicted file count** — `max(15s, 5s × files)`, capped at 60s. Wall time here is git's merge computation, not the pipe write, so it tracks how many files git has to merge. 15s is a floor that is generous for one conflicted text file and thin for a dozen files or a cold object cache — which is where the preview is worth the most.

**Debug a failed pipeline:**
```bash
./supertool 'gl-pipeline:12345:failed'   # only the failed jobs + their IDs/URLs
# or, while it's still running, what's left:
./supertool 'gl-pipeline:12345:active'   # only running/pending jobs
# then dig into a failed job by ID:
./supertool 'gl-job:67890'
# if you need more log context:
./supertool 'gl-job:67890:raw:1:150'
# the failure is nearly always at the end — ask for the tail directly:
./supertool 'gl-job:67890:raw:-40'
# or hunt a specific marker yourself (regex, ±context, never silent-empty):
./supertool 'gl-job:67890:grep:applied_rectors'
```

### Reading a range

`raw` is reached as a fallback when `:fail` did not surface the cause, so `START` is a guess — and because the interesting lines are found by walking backwards from the end, the guess is nearly always aimed at the tail.

```bash
./supertool 'gl-job:6933325:raw:-40'        # last 40 lines — no need to know the total
./supertool 'gl-job:6933325:raw:3630:3674'  # absolute range, 1-indexed inclusive
```

**A range past the end of the log returns the tail of the width requested, and says so** ([#487](https://github.com/Digital-Process-Tools/claude-supertool/issues/487)). It used to decline:

```
## Raw — start (3830) > total (3674); nothing to show
```

which cost a whole round-trip to re-read a bound the very same response had already printed two lines above (`Log: 3674 lines total`). It now answers:

```
## Raw — requested 3830-3870 is past end of log (3674 lines); showing the last 41 lines instead

## Raw lines 3634-3674 of 3674
```

**The notice is not decoration.** Handing back 3634–3674 to a caller who asked for 3830–3870 without saying so is different data than was requested — the same disease one level down. Every raw response states the range it actually returned; when that is not the range asked for, it says that too. With no `END`, the fallback width is the `lines` config (default 80).

`:raw:-N` refuses an `END` (`raw:-40:50` has two contradictory anchors) and clamps to the log length, so `-500` on a 200-line log is the whole log. An **inverted** range (`raw:10:5`) is refused with exit 1 rather than slicing to nothing under a `## Raw lines 10-9 of 20` header. An **empty** log is reported as `## Raw — the log is empty (0 lines)` — an absence in the world, distinct from a request the op could not serve.

`gh-job` behaves identically.

### Per-job failure patterns + resolution op

`gl-job`'s default failure view searches `error_patterns` (a flat list). For repos
with several CI tools, configure a per-job-name table so each job type gets tighter
patterns **and** a one-line fix command. In `.supertool.json`:

```json
{
  "ops": {
    "gl-job": {
      "job_patterns": [
        { "job": "rector", "patterns": ["applied_rectors"], "resolution": "rector_ci_apply:{id}" },
        { "job": "phpstan", "patterns": ["🪪", "notSubtype"] }
      ]
    }
  }
}
```

The first entry whose `job` regex matches the job name wins; no match falls back to
the flat `error_patterns`. When a matched entry has a `resolution`, `gl-job` prints
`Resolve: ./supertool '<op>'` with `{id}` replaced by the job id — e.g. a failed
`rector` job ends with `Resolve: ./supertool 'rector_ci_apply:67890'`. Project config
deep-merges into the preset op, so adding `job_patterns` keeps the built-in `cmd`.

### The cause, not just the wreckage

A stack trace is the *consequence* of a failure; the line that says why sits above
it. So alongside `error_patterns`, `gl-job` always matches a built-in set of **cause
markers** — lines that state a reason rather than show its fallout:

`Caused by` · an FQCN-shaped `…Exception: ` / `…Error: ` message · `SQLSTATE[` ·
`In <File>.php line N:` · `Exit Code: N` · `Fatal error:` · `Segmentation
fault/violation` · `Allowed memory size … exhausted` · `…CrashedException`

Their window leans downward — 2 lines above, `error_context` below — because a cause
is followed by its message body. Cause markers are matched **in addition to** the
patterns in play, including a per-job `job_patterns` entry: a table tuned for one
tool's output goes on narrowing that tool's noise, but it cannot suppress the reason
a job died. Set `GL_JOB_CAUSE_MARKERS=0` to disable them, `GL_JOB_CAUSE_CONTEXT_BEFORE`
to change the leading context.

### A failed job never reports nothing

`## No error patterns matched` is printed **only for jobs that did not fail**. When
GitLab says a job `failed` and nothing matched, the output says so instead:

```
## FAILED — no error pattern matched
Job status is `failed`: something did go wrong. supertool could not classify it,
which means a pattern is missing here — not that the log is clean. …
Patterns tried: ERROR, FAILURES!, Fatal, … (+ built-in cause markers)
Last step entered: build_assets

## Log tail (last 40 lines of 1612)
  …
Next:  ./supertool 'gl-job:6929217:raw'  or  'gl-job:6929217:grep:PATTERN'
```

A failed job always has a reason; not finding it is a gap in this tool, and the
output has to read that way rather than as silence — a crashed job reporting no
errors reads as green, which is the worst output a failure tool can produce. The
tail length is `GL_JOB_UNMATCHED_TAIL_LINES` (default 40). This applies to `:fail`
and to the default view alike.

### PHPUnit failures are kept whole

A matched line is shown with `error_context` lines around it, which is right for a
stack trace and wrong for a PHPUnit assertion failure — there the middle *is* the
evidence (the rendered HTML/JSON, the expected-vs-actual diff) and the head/tail are
just `Failed asserting that '` and `does not contain "X"`. So a failure block, from
its `N) Class::method` header to the trailing `/path/File.php:LINE` frames, is never
elided in the middle: touching it anywhere pulls in the whole block. Blocks longer
than `GL_JOB_PHPUNIT_BLOCK_MAX_LINES` (default 500) keep their head and tail, and
every gap marker states the count — `... (44 lines elided)` — so a trimmed view can
never be mistaken for a complete one.

Expansion is budgeted across the whole log by `GL_JOB_PHPUNIT_TOTAL_MAX_LINES`
(default 2000), because one broken shared component fails dozens of tests at once.
Past the budget whole failures are dropped in file order rather than gutted — a
dropped failure falls back to its ordinary pattern window, so nothing disappears —
and the view ends with `... (3 of 21 PHPUnit failures not shown in full — raise
GL_JOB_PHPUNIT_TOTAL_MAX_LINES=N)`.

**Check an issue before starting work:**
```bash
./supertool 'gl-issue:999' 'gl-mr:feature/my-branch:status'
```
Gets the full issue context and checks whether an MR already exists for the branch.

**Triage your open MRs:**
```bash
./supertool 'gl-mrs'                          # my MRs: pipeline, approval, age, watch-state
./supertool 'gl-mrs:reviewer=@me'             # MRs I'm reviewing
./supertool 'gl-mrs:author=@me,failed,iids'   # bare ids of my failing MRs
```
The last form feeds the [`watch`](watch.md) supervisor: pipe failing-MR ids straight into background pollers.

## Configuration

`gl-mrs` enrichment is tunable (parallelism, how many MRs to enrich, page size):

```json
{
  "ops": {
    "gl-mrs": {
      "cmd": "python3 {path}gitlab/mrs.py {args}",
      "enrich_workers": 8,
      "enrich_cap": 40,
      "per_page": 50
    }
  }
}
```

The list endpoint omits pipeline, approval, and diff data, so each MR costs a couple of `glab api` calls (MR detail + approvals, plus failed-job names for failing MRs). `enrich_workers` threads those calls; `enrich_cap` bounds how many MRs get enriched; `per_page` bounds how many are listed.

`gl-job` error pattern search and log tail length are configurable:

```json
{
  "ops": {
    "gl-job": {
      "cmd": "python3 {path}gitlab/job.py {args}",
      "lines": 120,
      "error_patterns": "ERROR,FAILURES!,Fatal,Failed asserting,PHP Warning,PHP Notice",
      "error_context": 10
    }
  }
}
```

Otherwise inherits from `glab auth status` — no project-specific tokens needed.

### Default project for `gl-issue-create`

`gl-issue-create` needs a `project` in every payload. To omit it, set a default:

```json
{
  "defaults": {
    "gitlab_project": "fdavid/dvsi"
  }
}
```

Resolution order (most specific wins): explicit `project` in the payload → `defaults.gitlab_project` → the `origin` git remote when its host contains `gitlab`. In a GitLab checkout the remote covers it with zero config; the explicit default is for when `origin` points elsewhere.

## Authoring notes

Preset JSON: `presets/gitlab.json`. Helper scripts: `presets/gitlab/`. `gl-mr` accepts either an MR number or a branch name — it resolves branches to MRs automatically.
