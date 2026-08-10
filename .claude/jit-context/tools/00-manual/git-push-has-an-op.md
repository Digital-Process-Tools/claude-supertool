---
title: "`git push` has an op"
tool: Bash
match: ~(^|[;&|\n] *)(rtk +)?(command +)?git[[:space:]]+([^;&|\n]*[[:space:]])?push
mode: block
---

`git-push` — pushes the **current branch**, so `cd` into the worktree instead of `-C`.

| Instead of | Use |
| --- | --- |
| plain push | `git-push` |
| `--force-with-lease` | `git-push:force-with-lease` |
| first push after `git worktree add -b` | `git-push:set-upstream` |
| push onto the tracked ref on purpose | `git-push:to-upstream` |
| `--no-verify` | `git-push:no-verify` |
| push + watch pipeline | `git-push:watch` |

- **A zero exit is not a push.** The op reads the post-push sha back off the remote via `ls-remote` and labels it verified. Filed instances both ways: `git push -q` + unconditional success echo while the remote head had not moved; an rtk-wrapped tag push killed by SIGPIPE that left no tag.
- `git worktree add -b <new> <base>` leaves the branch tracking `<base>` — every `st-wt/NNN` starts wrong. `:set-upstream` / `:to-upstream` make that explicit.
- No op for tags: `gh api -X POST .../git/refs -f ref=refs/tags/vX.Y.Z -f sha=$FULL_SHA`, verify with `ls-remote --tags`. Ref delete is `gh api -X DELETE` (skips the per-deletion pre-push suite).
