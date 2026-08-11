---
title: ".github/workflows/ — CI shape and log-reading"
match: ".github/workflows/"
mode: once, remind
---

# Workflows that exist (only these three)

| File | Trigger | What it does |
|---|---|---|
| `tests.yml` | `push: [master]`, `pull_request` | `pytest` matrix (12 legs), `coverage`, `notifiers` |
| `slow-tests.yml` | `schedule: 0 6 * * *`, `workflow_dispatch` | `pytest -m slow`, ubuntu/3.12 only |
| `changelog.yml` | `pull_request: [opened, synchronize, reopened, labeled, unlabeled]` | fragment/`CHANGELOG.md` policy check |

A workflow not in this list, or a job whose trigger doesn't match the event you're looking at, is **UNKNOWN — not a pass**. `slow` tests never run on a normal push; don't read their absence from a PR's checks as green.

# `pytest` job (`tests.yml:17-129`)

- Matrix: `{ubuntu,macos,windows}-latest × py{3.9,3.10,3.11,3.12}` = 12 legs. `notifiers` job: ubuntu+macos only (no windows — AF_UNIX socket).
- Command: `pytest -m 'not slow and not benchmark' --tb=no --no-cov --durations=25 --junit-xml=junit.xml -q` (line 117).
- **`--tb=no` means no traceback ever reaches the raw pytest log.** Read the failure via the next step instead.
- Next step (always runs): `python .github/scripts/junit_summary.py junit.xml` — parses `junit.xml`, prints the full failing assertion. **This is the log to read, not the pytest step above.**
- `coverage` job: separate, ubuntu-only, one leg — not per-OS. Gate: `.github/scripts/coverage_gate.py`.
- Job timeouts are deliberate hang-guards (30min pytest, 20min coverage, 10min notifiers), sized ~3x the worst observed run — a timeout firing means a hang, not just "slow runner."

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
