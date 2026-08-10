---
title: "`gh pr view` / `gh pr merge` have ops"
tool: Bash
match: ~(^|[;&|\n] *)(rtk +)?gh[[:space:]]+pr[[:space:]]+(view|merge)
mode: block
---

| Instead of | Use |
| --- | --- |
| `--json state,mergeable,statusCheckRollup` | `gh-pr:N:status` |
| `--json body`, `Closes` lines | `gh-pr:N`, `gh-pr:N:full` |
| `--json files` | `gh-pr:N:diff` |
| one file's hunks | `gh-pr:N:diff:PATH` |
| `gh pr merge` | `gh-pr-merge:N:squash` (add `|force`; bare = preview) |

- `gh-pr-merge` refuses on the #454 arithmetic, reads `state`/`mergedAt`/`mergeCommit` back off the remote, checks each linked issue against `closingIssuesReferences`, then reports master after the squash.
- A bare PR number resolves against the CWD's forge — `gh pr view 1057` once answered about a GitLab MR. The ops honour `repo:OWNER/NAME`.
- Measured 2026-08-10: one tick ran 12 `gh pr view` invocations, all answered by an op.
- Tell you skipped an op: a `--jq` against a `gh` read.
