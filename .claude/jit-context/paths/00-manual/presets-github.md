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

# Open defects — check before trusting output

- **#1181**: `gh-pr:N:status` prints `TALLY UNVERIFIED` on every PR because the Copilot review check-run has no workflow to count against. Code tries to stay silent when nothing is missing (`_reconcile_checks` in `pr.py:219`) — this is the case that slips through.
- **#1207**: `gh-prs` defaults to `author=@me` (deliberate, disclosed in footer) but a maintainer wants every author. Use `gh-prs:anyauthor` or dependabot/outside-contributor PRs are invisible.
- **#1180**: `presets/github/issues.py:251` — `parts[2].endswith("github.com")` matches `evilgithub.com`.

# `gh-issues` flags

`DEFAULT_PER_PAGE = 50` (`issues.py:73`) — output says `capped at --limit 50 — more may exist, raise with per=N`. Use `per=100`.
Other flags (`_FLAGS`, `issues.py:82`): `nopipe`, `iids`, `external`, `stale`, `nomilestone`. Also `label=`, `milestone=` key=value filters.

# Repo targeting

`repo:OWNER/NAME` as the **leading** op sets `SUPERTOOL_REPO` for every `gh-*` op in the call (`presets/_repo_target.py`, #673) — without it, ops read the cwd's git remote. `gh-pr:N:diff` in particular carries `--repo` through, so running it from the wrong directory silently answers about the wrong repo if no `repo:` target is given (#677/#678).
