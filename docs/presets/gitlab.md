# gitlab

GitLab ops via the `glab` CLI. Replaces the 3-5 separate `glab` calls needed to review an MR (branch, pipeline, approvals, diff, comments) and debug a failed job. Each op returns a full dashboard so the agent doesn't need a follow-up turn to locate the failure.

## Requires

[`glab` CLI](https://gitlab.com/gitlab-org/cli) installed and authenticated.

## Ops

| Op | Syntax | What it returns |
|----|--------|-----------------|
| `gl-issue` | `gl-issue:NUMBER[:full]` | Issue metadata, description, comments (truncated by default), related MRs. `:full` disables truncation |
| `gl-mr` | `gl-mr:NUMBER_OR_BRANCH[:status]` | MR dashboard: branch, pipeline, reviewer/approval state, linked issue, diff stat, comments. `:status` returns slim merge-state only |
| `gl-mrs` | `gl-mrs[:filters,flags]` | MR triage board, sorted failing-first then stalest. Per MR (enriched in parallel): pipeline status (a failure shows the failed **job name** = the failure class), approval state, age, diff size, watch-state cross-reference, and `conflict`/`draft`/`threads` flags — plus an actionable footer. Filters (comma-sep): `author`/`reviewer`/`assignee`/`label`/`milestone`/`state`/`per`. Flags: `nopipe` (skip enrichment), `iids` (bare id list), `failed` (only failing) |
| `gl-pipeline` | `gl-pipeline:NUMBER` | Pipeline job list grouped by stage with pass/fail status and failed job IDs |
| `gl-job` | `gl-job:NUMBER[:raw[:START[:END]]]` | Job failure detail: MR context + error pattern search + log tail. `:raw` dumps the full trace; `:raw:START:END` slices lines (1-indexed, inclusive) |

## Common workflows

**Review an MR before merging:**
```bash
./supertool 'gl-mr:42'
```
Returns approval state, pipeline status, diff stat, and all comments in one call.

**Debug a failed pipeline:**
```bash
./supertool 'gl-pipeline:12345'
# find the failed job ID, then:
./supertool 'gl-job:67890'
# if you need more log context:
./supertool 'gl-job:67890:raw:1:150'
```

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
