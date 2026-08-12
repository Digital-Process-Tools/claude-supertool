---
title: "`git -C <path>` is `cwd:` plus an op"
tool: Bash
match: ~(^|[;&|\n])[[:space:]]*(rtk[[:space:]]+)?(command[[:space:]]+)?git[[:space:]]+-c[[:space:]]+[^=[:space:]]*/[^[:space:]]*[[:space:]]+(diff|log|status[^;&|\n]*[[:space:]]-)
mode: block
---

**Do not drive another worktree with `git -C`.** `cwd:PATH` is the first op of the call and applies to the whole call, so the path is typed once instead of once per command.

| Instead of | Use |
| --- | --- |
| `git -C W diff` | `supertool 'cwd:W' 'git-diff'` (`:branch[:BASE]`, `:full` for hunks) |
| `git -C W log master..HEAD` | `supertool 'cwd:W' 'git-diverge:BRANCH[:BASE]'` |
| `git -C W status --porcelain` to see if a tree is busy | `supertool 'git-worktrees'` — three states, and `cannot tell` is not `idle` |
| several trees in a row | one `git-worktrees` call answers all of them |

**Only what no `replaces` entry claims.** Bare `git -C W status`, `commit`, `push` and `worktree list` are the shipped guard's — this rule fired on them too until #1438, so each was refused twice with two different messages. A flagged `status` is still here, because the guard's `unless_flag` declines all four spellings; a flagged `push`/`commit`/`worktree list` is refused by neither, deliberately, since there is no row above to offer. The coupling that creates is held by `tests/test_jit_rule_retirement_1376.py`: a command the registry claims *and* this `match` still fires on is a red test naming both.

`cwd:` moves repo paths only: a relative `@payload` reference still resolves against the directory the call was made from, so pass payload paths absolutely.

**Measured.** #850 was filed by Florian, not by me, after I typed `git -C ~/Documents/st-wt/810`, then `842`, then `804` across one afternoon and filed nothing — repetition never *feels* like a workaround, which is why it needs a rule rather than a habit. The ops also carry what the raw command does not: `git-status` names the kind of divergence, `git-worktrees` names each tree's tracker and merge state in four and three states.

A config override (`git -c key=value`) is untouched — the subject is lowercased before matching, so the two spellings are indistinguishable by case, and what separates them is the argument: this fires only when it is a path rather than a `key=value`.
