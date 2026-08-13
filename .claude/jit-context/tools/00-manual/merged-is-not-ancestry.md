---
title: "Do not ask git whether a branch is merged — this repo squash-merges"
tool: Bash
match: ~(^|[;&|\n])[[:space:]]*(rtk[[:space:]]+)?git[[:space:]]+(branch|for-each-ref)[^|]*--merged
mode: block
require: --merged
---

**Use `git-worktrees`.** It owns this question, for every worktree at once, with the branch, the path, the tracker state and an occupancy verdict alongside it:

```
supertool 'git-worktrees'              # the whole fleet
supertool 'git-worktrees:<PATH>'       # one tree, exit 0 only for idle
```

## Why the raw command is blocked rather than discouraged

`--merged` is an **ancestry** test. This repo squash-merges every PR, and a squash creates a commit with no parent link to the branch — so a fully merged branch is not an ancestor of `master` and `--merged` never names it.

It does not fail loudly. It returns a short, well-formed, wrong list:

- `git branch -r --merged` reported **4** on a repo holding **99** remote branches, **96** of them merged.
- 2026-08-09, six live worktrees: three fully-merged branches (`fix/1207`, `fix/1216`, `docs/contributor-skill`, merged as #1212, #1217, #1215) were absent from the merged set. Read the natural way, that says three branches hold unproposed work — an argument for opening three redundant PRs.

**A short answer and a correct answer look identical here**, which is why this blocks.

## The same trap in `git diff`

`git diff origin/master...HEAD` diffs against the **merge-base**, which predates the squash, so every already-merged line reappears as an addition. On `docs/contributor-skill` it claimed **1384 added lines across 5 files** — all five already on master.

If you need to know whether specific content landed, ask master for the content, not for the ancestry:

```bash
git cat-file -e origin/master:path/to/file && echo present || echo ABSENT
```

## When you genuinely need the raw list

GitHub is authoritative about squashes; git is not:

```bash
gh pr list --state merged --limit 400 --json headRefName -q '.[].headRefName'
```

Intersect with the live branch list. What is left over has no merged PR and is real work.
