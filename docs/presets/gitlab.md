# gitlab

GitLab ops via the `glab` CLI. Replaces the 3-5 separate `glab` calls needed to review an MR (branch, pipeline, approvals, diff, comments) and debug a failed job. Each op returns a full dashboard so the agent doesn't need a follow-up turn to locate the failure.

## Requires

[`glab` CLI](https://gitlab.com/gitlab-org/cli) installed and authenticated.

## Ops

| Op | Syntax | What it returns |
|----|--------|-----------------|
| `gl-issue` | `gl-issue:NUMBER[:full]` | Issue metadata, description, comments (truncated by default), related MRs. `:full` disables truncation |
| `gl-mr` | `gl-mr:NUMBER_OR_BRANCH[:status\|:full]` | MR dashboard: branch, pipeline, reviewer/approval state, linked issue, diff stat, per-file name-status (`A`/`D`/`R`/`M`) list, comments. The file list is the high-signal "what got removed?" scan; capped at 50 files by default with a `… +N more` marker. `:status` returns slim merge-state plus the `source -> target` branch line only; `:full` uncaps the file list (paginating up to 500) and the comments |
| `gl-mrs` | `gl-mrs[:filters,flags]` | MR triage board, sorted failing-first then stalest. Per MR (enriched in parallel): pipeline status (a failure shows the failed **job name** = the failure class), approval state, age, diff size, watch-state cross-reference, and `conflict`/`draft`/`threads` flags — plus an actionable footer. Filters (comma-sep): `author`/`reviewer`/`assignee`/`label`/`milestone`/`state`/`per`. Flags: `nopipe` (skip enrichment), `iids` (bare id list), `failed` (only failing) |
| `gl-pipeline` | `gl-pipeline:NUMBER[:active\|:failed]` | Pipeline job list grouped by stage with pass/fail status and failed job IDs. The default board collapses the `manual`/`created`/`skipped` bulk to a one-line count so the running/done/failed jobs aren't buried. `:active` shows only running/pending jobs ("what's still going"); `:failed` shows only failed jobs plus their job IDs/URLs ("what broke") |
| `gl-job` | `gl-job:NUMBER[:raw[:START[:END]]\|:grep:PATTERN]` | Job failure detail: MR context + error pattern search + log tail. `:raw` dumps the full trace; `:raw:START:END` slices lines (1-indexed, inclusive); `:grep:PATTERN` runs an ad-hoc regex over the trace (literal fallback on bad regex, ±context, names the pattern + tail on no-match — never silent-empty). Cause markers are always matched on top of the configured patterns, and a *failed* job that matches nothing is reported as unclassified with a log tail rather than as "no errors" |

## Common workflows

**Review an MR before merging:**
```bash
./supertool 'gl-mr:42'
```
Returns approval state, pipeline status, diff stat, and all comments in one call.

**Debug a failed pipeline:**
```bash
./supertool 'gl-pipeline:12345:failed'   # only the failed jobs + their IDs/URLs
# or, while it's still running, what's left:
./supertool 'gl-pipeline:12345:active'   # only running/pending jobs
# then dig into a failed job by ID:
./supertool 'gl-job:67890'
# if you need more log context:
./supertool 'gl-job:67890:raw:1:150'
# or hunt a specific marker yourself (regex, ±context, never silent-empty):
./supertool 'gl-job:67890:grep:applied_rectors'
```

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
