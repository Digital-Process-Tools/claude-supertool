---
title: "Piping a supertool op through head/tail/sed selects against the answer"
tool: Bash
match: ~(^|[;&|\n])[[:space:]]*(rtk[[:space:]]+(proxy[[:space:]]+)?)?(python3?[[:space:]]+(-m[[:space:]]+)?)?([^[:space:]]*/)?supertool(\.py)?[[:space:]]([^;&\n]|&[^&[:space:]])*[[:space:]'"]\|[[:space:]]*(head|tail|sed|cut|awk)
mode: block
---

**Do not cut a supertool op's output.** The ops are already compressed and put
the meaning at the **top** — header, then meta (`state`, `mergeable`, the summed
check tally, `scanned N files`), then the body. `tail` selects against the header
where the verdict lives; `head` drops the end of every `gh-issue:N` body,
including a `Comments (1)` block that was the whole reason an issue was reopened.

**Narrow the op, never the output** — quote these as the op string, do not filter:

```
gh-pr:1208:status                not  gh-pr:1208:full | head
grep:PAT:PATH:10:2               not  grep … | head -20
read:PATH:::grep=PATTERN         not  read:PATH | grep
gh-job:N:fail                    not  gh-job:N:raw | tail
```

**Measured.** Three self-inflicted cuts in six hours on 2026-08-09: a `grep`
head that dropped 3 of 5 verifications into a brief unchecked, and two
`git-status` cuts that removed the working-tree section — the second an hour
after filing the issue about the first. It blocks rather than reminds because
the same rule, written and said out loud twice, was skimmed each time.

**Narrower, never precise.** The bar must sit against whitespace or a closing
quote, at command position. Still matched: a heredoc body line that *begins*
with a piped call, because `^`-alternation cannot tell a body from a command.
Still unmatched: an env-var prefix and a command substitution before the call.
The span stops at `;` and `&&`, so a *later* command's `| tail` no longer refuses
the op (#1565); `|` and `2>&1` stay inside it, because a real cut often has both.
The cost, measured not assumed: a `;` or `&&` **inside the op's own argument**
now ends the span too, so `'grep:a && b:.'` piped to `tail` goes unseen — the
same shape as the bar inside an argument, which was already unmatched. A missed
block is the safe direction here; a wrong one teaches routing around the block.
The reasoning is in #1415,
#1426, #1430 and #1433, not here: this body is re-injected in full on every
match, false ones included, so its length is the price of every wrong block.
