---
title: "Raw `git push` skips the receipt that says whether it landed"
tool: Bash
match: ~(^|[;&|\n] *)(rtk +)?(command +)?git[[:space:]]+([^;&|\n]*[[:space:]])?push
mode: block
---

Use **`git-push`**, and its flags for the cases that used to need a raw line.

| Instead of                                   | Use                        |
| -------------------------------------------- | -------------------------- |
| a plain push                                 | `git-push`                 |
| `--force-with-lease`                         | `git-push:force-with-lease`|
| a first push from `git worktree add -b`      | `git-push:set-upstream`    |
| pushing onto a tracked ref on purpose        | `git-push:to-upstream`     |
| `--no-verify`                                | `git-push:no-verify`       |
| push then watch the pipeline                 | `git-push:watch`           |

The op pushes the **current branch**, so `cd` into the worktree rather than reaching in with `-C`.

## Why blocking

**A zero exit is not a push.** `git-push` reads the post-push sha back off the real remote with
`ls-remote` and labels it verified or unverified — a sha it did not read is never printed as though
it were. That is the whole point: this repo has a filed instance of `git push -q` followed by an
unconditional `echo "pushed"` printing success while the remote head had not moved, and a separate
one where the `rtk` wrapper killed a tag push with `process terminated by signal 13` and the tag
simply did not exist. Both read exactly like success.

The receipt also carries what you were going to ask next anyway: remote before and after, ahead and
behind, the PR and its pipeline, mergeability, the count of uncommitted leftovers, and the watch
command. A non-fast-forward auto-rebases and hands back `git-conflicts` on a conflict instead of a
wall of git output.

**The upstream trap is the specific one that bites here.** `git worktree add -b <new> <base>` leaves
the new branch tracking `<base>`, which is where every `st-wt/NNN` branch starts, so a plain push
either goes to the wrong ref or is refused with advice the caller cannot act on. `:set-upstream` and
`:to-upstream` are that decision made explicitly, and asking for both is refused by name rather than
resolved by precedence.

## What has no op yet

Tags. `gh api -X POST repos/OWNER/REPO/git/refs -f ref=refs/tags/vX.Y.Z -f sha=$SHA` with a **full**
sha, then verify with `ls-remote --tags`. Deleting a remote ref is `gh api -X DELETE`, which also
skips the pre-push hook that would otherwise run the entire suite once per deletion.

## Writing this match

`\n` is in the separator class on purpose. The matcher tests the whole command, and awk anchors `^`
at the start of the **string**, not of each line — so without it a push on the second line of a
multi-line script is invisible. That is not hypothetical: the invocation that prompted this rule was
`command git -C "$W" push`, three lines into a heredoc, with `command` in front of it to bypass the
`rtk` wrapper.

Unlike `\s`, which this awk silently drops, `\n` is a valid string escape and survives into the
compiled pattern. Verified, both directions, before this rule was indexed.
