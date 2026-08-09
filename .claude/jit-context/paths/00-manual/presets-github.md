---
title: "presets/github/ — GitHub op quirks"
match: "presets/github/"
mode: once, remind
---

# Location

Directory is `presets/github/`. **`presets/gh/` does not exist** — `gh-` is only the op-name prefix.

# Check-state buckets (`presets/_checks.py`)

`_checks.summarize()` sums every leg to the leg count — no state is dropped (#445/#454).

| Bucket    | States |
|-----------|--------|
| PASSED    | `SUCCESS` |
| FAILED    | `FAILURE`, `FAILED`, `ERROR`, `STARTUP_FAILURE`, `TIMED_OUT`, `ACTION_REQUIRED` |
| PENDING   | `IN_PROGRESS`, `RUNNING`, `QUEUED`, `PENDING`, `WAITING`, `REQUESTED`, `EXPECTED`, `CREATED`, `SCHEDULED`, `PREPARING` |
| BENIGN    | `SKIPPED`, `NEUTRAL`, `MANUAL` |
| other/red | `CANCELLED` and anything unrecognized — counts red on triage boards, never silently fine |

`TIMED_OUT`/`ACTION_REQUIRED` are FAILED, not benign — don't lump them with SKIPPED.

# Open defects — ask the tracker, do not read a list here

This section used to enumerate open defects. On 2026-08-09 **all four of them were closed** and the list was still being injected into every call that touched this directory — including one telling the reader to pass `gh-prs:anyauthor` to widen a board whose default had already been widened, which is worse than stale, it is inverted.

A hand-maintained list of open defects inside an auto-injected file cannot stay true, because nothing closes the loop when the defect is fixed. So it is a query now:

```
supertool 'gh-issues:label=lane-tracker-ops,per=100'
```

For the record, since the entries were load-bearing while they lasted — closed and verified against master on 2026-08-09:

| Was | Closed by | Now |
| --- | --- | --- |
| #1180 — `parts[2].endswith("github.com")` matches `evilgithub.com` | #1212 | `_is_github_host()` (`issues.py:242`) — exact host or `.`-boundary subdomain, userinfo and port stripped |
| #1207 — `gh-prs` defaults to `author=@me` | #1212 | **the bare op is the whole repo**; `author=@me` is a filter you write |
| #1181 — `TALLY UNVERIFIED` on every PR | #1212 | the tally cap was the cause |
| #1202 — `required()` inert on 13 adapters | #1213 | all 13 route through `refusal.absent()` |

**`gh-prs` is repo-wide by default. Do not add `anyauthor` expecting it to widen anything** — it is accepted and names what the bare op already does. `gl-mrs` is the one that still defaults to `author=@me` (`presets/gitlab/mrs.py:153`).

# `gh-issues` flags

`DEFAULT_PER_PAGE = 50` (`issues.py:73`) — output says `capped at --limit 50 — more may exist, raise with per=N`. Use `per=100`.
Other flags (`_FLAGS`, `issues.py:82`): `nopipe`, `iids`, `external`, `stale`, `nomilestone`. Also `label=`, `milestone=` key=value filters.

# Repo targeting

`repo:OWNER/NAME` as the **leading** op sets `SUPERTOOL_REPO` for every `gh-*` op in the call (`presets/_repo_target.py`, #673) — without it, ops read the cwd's git remote. `gh-pr:N:diff` in particular carries `--repo` through, so running it from the wrong directory silently answers about the wrong repo if no `repo:` target is given (#677/#678).
