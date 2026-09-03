---
title: ".github/workflows/ — CI shape and log-reading"
match: ".github/workflows/"
---

# Workflows that exist (only these three)

| File | Trigger | What it does |
|---|---|---|
| `tests.yml` | `push: [master]`, `pull_request` | four jobs: `pytest` matrix (12 legs), `coverage`, `lint-new`, `notifiers` |
| `slow-tests.yml` | `schedule: 0 6 * * *`, `workflow_dispatch` | `pytest -m slow`, ubuntu/3.12 only |
| `changelog.yml` | `pull_request: [opened, synchronize, reopened, labeled, unlabeled]` | fragment/`CHANGELOG.md` policy check |

A workflow not in this list, or a job whose trigger doesn't match the event you're looking at, is **UNKNOWN — not a pass**. `slow` tests never run on a normal push; don't read their absence from a PR's checks as green.

# `tests.yml` jobs

Cited by job and step name, never by line number — `tests.yml:117` was the pytest command until it became line 239, and a stale citation reads exactly like a live one (#2216).

- `pytest` — matrix `{ubuntu,macos,windows}-latest × py{3.9,3.10,3.11,3.12}` = 12 legs, 30min timeout. Command: `pytest -m 'not slow and not benchmark' --tb=no --no-cov --durations=25 --junit-xml=junit.xml -q`. **`--tb=no` means no traceback ever reaches the raw pytest log.** The next step, `Show failing tests with full messages (always runs)`, runs `python .github/scripts/junit_summary.py junit.xml` and prints the full failing assertion. **That is the log to read, not the pytest step above.**
- `coverage` — ubuntu-only, one leg, 20min. Gate: `.github/scripts/coverage_gate.py`.
- `lint-new`, named `lint (files this PR adds, renames or modifies)` — ubuntu-only, 10min. ruff over the **diff**: a path this PR adds or renames has no history and is checked whole; a modified path is checked only against its own merge-base, so the 263 findings the tree-wide ignore holds back stay held back. It exists because the `pytest` job deliberately runs no `ruff check .` and the supertool `ruff` validator reports only what an edit *introduces* — PR #1473 merged 22/22 green with three unreachable imports in a file it created (#1481, widened to modified paths in #1849). **A red here has no `junit.xml`**; the ruff output is in the job log itself.
- `notifiers` — ubuntu+macos only, 10min, no windows (AF_UNIX socket). The matrix above runs pytest and nothing else (#557).

Timeouts are deliberate hang-guards sized ~3x the worst observed run — one firing means a hang, not just a slow runner.

# Reading a red leg

1. `supertool 'gh-job:N:fail'`, or `:raw` for the whole log. If that comes back empty for a genuinely failed job: `gh api repos/OWNER/REPO/actions/jobs/<id>/logs`, which no op maps.
2. Don't `gh run rerun <id> --failed` before reading the log — it destroys the evidence and costs the same API call.
3. **A single-platform red is usually real, not a flake.** Running score here: 10 genuine to 2 flakes.
4. Best discriminator: **which tests passed on that leg.** `2 failed, 4793 passed` = product not disarmed, fault is in fixtures/env, not a mass failure.

# Known Windows-specific failure classes

- `PermissionError` where POSIX raises `IsADirectoryError`.
- Hardcoded POSIX path literals in a test.
- `MAX_ARG_STRLEN` (128 KB per string) — applies to `envp` too, not just `argv`.
- Symlink creation needs Developer Mode / `SeCreateSymbolicLinkPrivilege`; ungated `symlink_to()` fails loudly instead of skipping (`tests/_symlink.py::require_symlink()`).

# Other

- `-X utf8` on pytest, not `PYTHONUTF8`/`PYTHONIOENCODING` env vars — the flag isn't inherited by subprocesses, so tests reproducing Windows encoding bugs on purpose stay unaffected.
- Every third-party action pinned to a commit SHA, not a floating tag (`tests/test_ci_action_pinning_925.py`).
