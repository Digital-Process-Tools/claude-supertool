---
title: "These ops answer a narrower question than you asked"
tool: Bash
match: ~supertool[^|]*'(gh-prs|gl-mrs|gh-issues|gl-issues|radar|watches)
mode: remind
---

Each of these has a default that **narrows the population**. The render says so in its footer — this fires because the footer gets skimmed.

| Op | Silent default | Widen with |
| --- | --- | --- |
| `gh-prs` / `gl-mrs` | `author=@me` — your PRs, not the repo's | `gh-prs:anyauthor,state=open` |
| `gh-issues` / `gl-issues` | caps at **50**, prints `capped at --limit 50 — more may exist` | `gh-issues:per=100` |
| `radar` | tiers come from the **CWD's** `.supertool.json`; the GitHub tier inherits `author=@me` | `cwd:~/Documents/claude-supertool` first, then widen the tier |
| `watches` | shows tracked pollers; ones predating the labelling are invisible **by design** | cross-check `pgrep -f 'presets/watch/'` before concluding a slot is free |

## What it costs when you skip the flag

- **2026-08-09** — two dependabot PRs (green, 5h) and one external contributor's PR (green, 1 day) were invisible on `gh-prs`. Found by a human opening GitHub in a browser.
- **2026-08-09** — `gh issue list --milestone v0.31.0` with no `--limit` returned one page. Reported as **31**; the real count was **72**.

## The general shape

A filtered board and an empty board render almost identically, and the difference is one footer line. Before treating any count from these as a fact about the world, ask **which population answered**.

`radar` also spawns and reaps pollers. `radar:--state` does neither (#957) and is safe inside a live worktree.
