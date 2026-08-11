---
title: "`gh pr view` / `gh pr merge` / `gh pr create` have ops"
tool: Bash
match: ~(^|[;&|\n])[[:space:]]*(rtk[[:space:]]+)?gh[[:space:]]+pr[[:space:]]+(view|merge|create)
mode: block
---

| Instead of | Use |
| --- | --- |
| `--json state,mergeable,statusCheckRollup` | `gh-pr:N:status` |
| `--json body`, `Closes` lines | `gh-pr:N`, `gh-pr:N:full` |
| `--json files` | `gh-pr:N:diff` |
| one file's hunks | `gh-pr:N:diff:PATH` |
| `gh pr merge` | `gh-pr-merge:N:squash` (add `|force`; bare = preview) |
| `gh pr create --body-file` | `gh-pr-create:@FILE` (`base` REQUIRED, never defaulted) |

- `gh-pr-merge` refuses on the #454 arithmetic, reads `state`/`mergedAt`/`mergeCommit` back off the remote, checks each linked issue against `closingIssuesReferences`, then reports master after the squash.
- A bare PR number resolves against the CWD's forge — `gh pr view 1057` once answered about a GitLab MR. The ops honour `repo:OWNER/NAME`.
- `gh-pr-create` reports whether any check actually STARTED — zero checks renders as `nothing has been created`, never as pending — and parses the body `Closes` lines with the same reader `gh-pr` uses, so a malformed one is caught at creation instead of after the merge.
- Measured 2026-08-10: one tick ran 12 `gh pr view` invocations and 3 raw `gh pr create` calls, all answered by an op.
- Tell you skipped an op: a `--jq` against a `gh` read, or a `--body-file`.
