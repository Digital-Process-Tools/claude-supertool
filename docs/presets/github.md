# github

GitHub ops via the `gh` CLI. Replaces the 4-6 separate `gh` calls needed to review a PR (branch, checks, approvals, diff, comments), debug a failed Actions run, and manage social activity (follows, stars). The PR and issue ops pack a full dashboard into one call — no follow-up turn needed to decide whether to merge or where the failure is.

## Requires

[`gh` CLI](https://cli.github.com) installed and authenticated (`gh auth login`).

## Ops

| Op | Syntax | What it returns |
|----|--------|-----------------|
| `gh-issue` | `gh-issue:NUMBER[:full]` | Issue metadata, description, all comments (truncated by default), linked PRs, image download. `:full` disables truncation |
| `gh-pr` | `gh-pr:NUMBER_OR_BRANCH[:status]` | Full PR dashboard: branch, checks, reviews/approval state, linked issue, diff stat, comments. `:status` returns slim merge-state only (~250 bytes) |
| `gh-prs` | `gh-prs[:author=@me,reviewer=@me,state=open,failed,nopipe,iids]` | PR triage board: your open PRs sorted failing-first then stalest. Per PR: check rollup (a failure shows the failing **check name**), approval state, age, diff size, watch-state, `draft`/`conflict`/`threads` flags + footer pointing at the first failing-and-unwatched PR. The gl-mrs twin. `iids` emits a bare number list for `watch-mine.sh` |
| `gh-run` | `gh-run:NUMBER` | Workflow run job list with statuses and failed step names |
| `gh-job` | `gh-job:NUMBER[:raw[:START[:END]]]` | Job failure detail: PR context + error pattern search + log tail. `:raw` dumps the full trace; `:raw:START:END` slices lines (1-indexed, inclusive) |
| `gh-follow` | `gh-follow:USERNAME` | Follow a GitHub user via the authenticated session |
| `gh-following` | `gh-following[:N]` | List users you follow (default 30) |
| `gh-batch-follow` | `gh-batch-follow:FILE` | Follow each username from a file (one per line, `#` comments). 1s delay between calls |
| `gh-star` | `gh-star:OWNER/REPO` | Star a repository |
| `gh-starred` | `gh-starred[:N]` | List repos you have starred (default 30) |
| `gh-batch-star` | `gh-batch-star:FILE` | Star each `OWNER/REPO` from a file (one per line, `#` comments). 1s delay between calls |
| `gh-find-followable` | `gh-find-followable:OWNER/REPO[|N]` | Discover candidate users to follow: pulls stargazers + contributors, deduplicates, filters orgs. Pipe output to a file then review before `gh-batch-follow` |
| `gh-find-starable` | `gh-find-starable:TOPIC[|N]` | Discover repos worth starring by topic, sorted by stars. Pipe output to a file then review before `gh-batch-star` |

## Common workflows

**Review a PR before merging:**
```bash
./supertool 'gh-pr:42' 'gh-run:NUMBER'
```
`gh-pr` gives you the full dashboard (approval, diff stat, comments); `gh-run` confirms all checks passed. Replace `NUMBER` with the run ID shown in `gh-pr` output.

**Debug a failed Actions job:**
```bash
./supertool 'gh-run:12345'
# find the failed job ID, then:
./supertool 'gh-job:67890'
# if you need the full log:
./supertool 'gh-job:67890:raw:1:100'
```

**Build a follow list from a repo's community:**
```bash
./supertool 'gh-find-followable:anthropics/claude-code|100'
# review the output, save to a file, then:
./supertool 'gh-batch-follow:.max/follow-list.txt'
```

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

## Authoring notes

Preset JSON: `presets/github.json`. Helper scripts: `presets/github/` — one Python file per op. `gh-find-followable` and `gh-find-starable` are discovery ops: they produce a list for human review, not an immediate action. Always review the file before running `gh-batch-follow` or `gh-batch-star`.
