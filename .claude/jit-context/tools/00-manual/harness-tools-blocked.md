---
title: "Read/Edit/Write/Grep/Glob go through supertool here"
tool: Edit|Write|Read|Grep|Glob|MultiEdit|NotebookEdit
match: ~.
mode: block
---

**This repo is supertool. Use it.** The harness file tools are blocked here on purpose — not because they are broken, but because every one of them is a strictly worse version of an op this repo ships, and a maintainer who does not call their own tool never finds its defects.

| Instead of | Use |
| --- | --- |
| `Read` | `read:PATH`, `read:PATH:OFF:LIM`, `read:PATH:::grep=PATTERN`, `around_line:PATH:LINE:N`, `between:SYMBOL:PATH` |
| `Grep` | `grep:PATTERN:PATH:LIMIT:CONTEXT`, `grep_around:PATTERN:PATH:N`, `grep:…:count` |
| `Glob` | `glob:PATTERN`, `tree:PATH:DEPTH`, `map:PATH`, `ls:PATH` |
| `Edit` | `edit:::OLD:::NEW:::PATH` or `edit:@-` with a TOML payload |
| `Write` | `paste:::PATH:::CONTENT` or `paste:@-` |
| several at once | one call, 6-7 ops — `read`, `grep`, `glob`, `map`, `around`, `between`, `tree` |

## Why this blocks rather than reminds

**Validators.** A mutating op runs the validator chain after the write and **rolls the file back on a syntax failure**. `Edit` does not. On this repo that is `py-syntax`, `ruff`, the changelog-fragment guard, the syntax-floor check — a broken fragment or an unparseable module is caught at write time instead of twenty minutes later on a 20-leg matrix.

**Round-trips.** 37 individual `Read` calls re-pay the cached prefix 37 times; six batched op calls pay it six. An agent that reads one file at a time burns its own context before it reaches the judgment it was delegated for.

**Receipts.** The ops report what they did and what is next — `scanned N files`, `no regex match; showing N literal matches`, `OFFSET is a skip count, not a start line`, the exact op to run instead. A silent tool teaches nothing.

## Inside a branch worktree

The global binary resolves to the live clone, so in `st-wt/NNN` it runs **master's core against your branch's presets** — it warns `mixed supertool trees` but still runs the wrong code. There:

```
python3 supertool.py 'op:args'
```

Everywhere else, including `~/Documents/claude-supertool` on master, bare `supertool` is correct.

`supertool 'ops'` lists everything.
