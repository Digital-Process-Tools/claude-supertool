---
title: "These ops narrow the population — check the footer"
tool: Bash
match: ~(^|[;&|\n])[[:space:]]*(rtk[[:space:]]+(proxy[[:space:]]+)?)?(python3?[[:space:]]+(-m[[:space:]]+)?)?([^[:space:]]*/)?supertool(\.py)?[[:space:]][^|]*'(gh-prs|gl-mrs|gh-issues|gl-issues|radar|watches)
mode: once,remind
---

| Op | Silent default | Widen with |
| --- | --- | --- |
| `gh-prs` | none — the bare op is the whole repo | nothing; `anyauthor` names what it already does |
| `gl-mrs` | `author=@me` | `gl-mrs:author=` |
| `gh-issues` / `gl-issues` | caps at **50** | `per=100` |
| `radar` | tiers from the **CWD's** `.supertool.json`; the GitLab tier applies `DEFAULT_FILTER` | `cwd:` first; `radar:--state` (read-only, spawns nothing) |
| `watches` | pollers predating the labelling are invisible **by design** | `pgrep -f 'presets/watch/'` |

A filtered board and an empty board render almost identically. Before a count becomes a fact: **which population answered?**

2026-08-09: `gh issue list --milestone v0.31.0` with no `--limit` reported **31**. The real count was **72**.
