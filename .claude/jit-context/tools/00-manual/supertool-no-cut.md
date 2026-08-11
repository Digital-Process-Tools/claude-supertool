---
title: "Piping a supertool op through head/tail/sed selects against the answer"
tool: Bash
match: ~(^|[;&|\n])[[:space:]]*(rtk[[:space:]]+)?(python3?[[:space:]]+)?([^[:space:]]*/)?supertool(\.py)?[[:space:]][^\n]*[[:space:]]\|[[:space:]]*(head|tail|sed|cut|awk)
mode: block
---

**Do not cut a supertool op's output.** The ops are already compressed — that is the premise — and they put the meaning at the **top**: header, then meta (`state`, `mergeable`, the summed check tally, `scanned N files`), then the body.

So the two cuts fail in opposite directions and both throw away the answer:

- **`tail`** shows whichever section happened to be last — validators output, a stash list, an MR's prose. It selects *against* the header where the verdict lives.
- **`head`** is not the safe half. `gh-issue:N` puts the body under the header, so `| head -90` silently drops the tail of every body — including a `Comments (1)` block that was the whole reason an issue was reopened.

**Three times on 2026-08-09, self-inflicted:**

| Cut | What it cost |
| --- | --- |
| `supertool 'grep:…' \| head -80` | dropped 3 of 5 verifications; two claims went into a brief unchecked |
| `git-status \| tail -12` | returned stashes and the MR block, no working-tree section — reported as reproducing a defect that was my own pipe |
| `git-status \| sed -n '1,25p'` | cut the working-tree section again, in the same session, an hour after filing the issue about it |

## What to do instead

**Narrow the op, never the output.**

```
supertool 'gh-pr:1208:status'          not  gh-pr:1208:full | head
supertool 'grep:PAT:PATH:10:2'         not  grep … | head -20
supertool 'read:PATH:::grep=PATTERN'   not  read:PATH | grep
supertool 'gh-job:N:fail'              not  gh-job:N:raw | tail
```

`[result]` lines exist so a one-line verdict survives a pipe. That is a floor for the summary — not a licence to truncate everything above it.

**The general rule, which is why this blocks rather than reminds:** when a read is about to become a fact, do not throw away part of it on the way in. A reminder gets skimmed at 18:30 on a long session; this one had been written, said out loud twice, and set in bold — and was still violated three times in six hours.
## What this pattern can and cannot see (#1415)

The match is pinned to command position, because until #1415 it was not — and on
2026-08-11 it refused **seven** commands that cut nothing: this repository's own
directory name inside a `cd` path (x3), a shell variable, a bar inside an op's own
argument (`grep:head|tail`), and a heredoc body quoting a piped example. The
anchoring idiom is the one `gh-pr-view-merge-have-ops.md` already used.

**It is still a regex, not a parser.** A heredoc line that *begins* with a supertool
call and a pipe still matches, because `^`-alternation cannot tell a body from a
command. Excluding quoted strings and heredocs by construction needs the tokeniser
the `guard` op uses, and the JIT matcher is `claude-jit-context`'s — a separate
repository. Read this rule as narrower, never as precise.
