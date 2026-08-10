---
title: "These ops answer a narrower question than you asked"
tool: Bash
match: ~supertool[^|]*'(gh-prs|gl-mrs|gh-issues|gl-issues|radar|watches)
mode: remind
---

Each of these has a default that **narrows the population**. The render says so in its footer — this fires because the footer gets skimmed.

| Op | Silent default | Widen with |
| --- | --- | --- |
| `gh-prs` | **none — the bare op is the whole repo** (#1207, shipped in #1212) | nothing to widen; `anyauthor` is accepted and names what it already does |
| `gl-mrs` | `author=@me` — your MRs, not the project's (`presets/gitlab/mrs.py:153`) | `gl-mrs:author=` or another role filter |
| `gh-issues` / `gl-issues` | caps at **50**, prints `capped at --limit 50 — more may exist` | `gh-issues:per=100` |
| `radar` | tiers come from the **CWD's** `.supertool.json`. The GitHub tier no longer narrows by author (#1230 — it had kept the default `gh-prs` dropped in #1207); the GitLab tier still applies `defaults.DEFAULT_FILTER`, spelled out in its own scope line | `cwd:` first; `radar:--state` prints the filter each tier resolved |
| `watches` | shows tracked pollers; ones predating the labelling are invisible **by design** | cross-check `pgrep -f 'presets/watch/'` before concluding a slot is free |

**The `gh-prs` row was inverted for an unknown number of sessions** — it told the reader to add `anyauthor` to widen a board that had already been widened, which is a reminder actively teaching a wrong fact. Corrected 2026-08-09 against `presets/github/prs.py:27` (*"The default is the whole repo… It used to be `author=@me`"*) and a live footer reading `no author filter (default)`.

**`radar` carried that defect for one release** and it is the op a maintainer tick opens with: `radar` printed `scope author=@me (default)` while `gh-prs` printed `no author filter (default)`, seconds apart, about one question. Fixed in #1230 by removing the narrowing from `_build_list_cmd` itself, so no third caller can inherit it. Check with `radar:--state`, which is read-only.

## What it costs when you skip the flag

- **2026-08-09** — two dependabot PRs (green, 5h) and one external contributor's PR (green, 1 day) were invisible on `gh-prs`. Found by a human opening GitHub in a browser.
- **2026-08-09** — `gh issue list --milestone v0.31.0` with no `--limit` returned one page. Reported as **31**; the real count was **72**.

## The general shape

A filtered board and an empty board render almost identically, and the difference is one footer line. Before treating any count from these as a fact about the world, ask **which population answered**.

`radar` also spawns and reaps pollers. `radar:--state` does neither (#957) and is safe inside a live worktree.
