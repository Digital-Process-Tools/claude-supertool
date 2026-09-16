---
title: "git-worktrees says [merged, ...] -- that is not the same claim as caught up with master"
tool: Bash
match: ~(^|[;&|\n])[[:space:]]*(rtk[[:space:]]+(proxy[[:space:]]+)?)?(python3?[[:space:]]+(-m[[:space:]]+)?)?([^[:space:]]*/)?supertool(\.py)?[[:space:]][^|]*'git-worktrees
mode: once,remind
---

**A `[merged, clean]` worktree can still be behind master.** `git-worktrees` reports whether that
tree's own commits are (transitively) merged, via squash-merge, not whether the branch has caught up
with everything master gained since. A branch reused across a multi-issue lane, after an earlier PR
from the same branch already merged, can read `[merged, clean]` and still be `2 ahead, 4 behind`.

Check `git-status`'s ahead/behind line before writing anything in a reused worktree. If behind:
`git fetch origin` then `git rebase origin/master` (not `reset --hard` or `checkout -B` — the
auto-mode classifier blocks both as destructive even when nothing unique would be lost). A rebase
conflict on already-merged content resolves with `git rebase --skip`; git's own patch-equivalence
detection drops an already-upstream commit without prompting.

Measured 2026-09 (#1253/#1868): a bundled dispatch left branch `fix/1253` at `[merged, clean]` after
an earlier PR (#2554) from the same branch merged, while master had moved on — working from that
state would have based new commits on stale content and produced a diff polluted by re-adding
already-merged changes.
