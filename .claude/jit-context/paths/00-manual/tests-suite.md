---
title: "tests/ — the full suite is non-hermetic"
match: "tests/"
---

# Before you run the full suite

**The suite is non-hermetic: its git-op tests corrupt the worktree they run in.** Not a warning about flakiness — it rewrites the index of the checkout you are standing in.

Observed 2026-08-09 in `st-wt/1205`: an agent committed its work, launched the full suite in its own worktree, and the index came back with nine paths staged as `D` (deleted) *and* present as `??` (untracked), plus eight stray `.coverage.*` shards. The commit survived; the working state did not.

## Run it in a disposable clone

```bash
git clone ~/Documents/claude-supertool /tmp/st-suite-$$ && cd /tmp/st-suite-$$
git remote set-url origin https://github.com/Digital-Process-Tools/claude-supertool.git
```

**The `set-url` is not optional.** A plain clone points `origin` at a local path, which reddens `test_live_board_over_this_repo` and `test_no_cli_at_all_is_not_an_absence_either`. Both fail identically on the base commit, so without it you manufacture two failures on every run and cannot tell them from real ones.

**Commit first — a clone copies commits, not your working tree.** Uncommitted work is simply absent from it, so the run is green about master and reads as green about your change. Measured 2026-08-13 in `st-wt/1521`: 12,112 passed against a diff that was not in the clone; the same suite over the commit found a real red. Add `-b <branch>` and check `git log --oneline -1` in the clone before trusting the number.

**Targeted runs in your own worktree are fine.** It is the full suite that does the damage.

# Two ways a test here lies about the platform

**Symlinks.** `tests/_symlink.py` exposes `require_symlink()`; use it. Windows without Developer Mode or `SeCreateSymbolicLinkPrivilege` raises `OSError` on `symlink_to()`, so an ungated test **fails** where every gated sibling **skips** — and a failure points at product code that is fine. Filed as #1205; the class was 28 sites across 18 files (#1232). You do not have to remember: `tests/test_symlink_gating_register_1232.py` registers every call site (69 at #1274) and goes red on a new ungated one, naming the file, the function and the fix. Its terminal count is a **subset**: only that helper's two forms produce a skip carrying the token, so a site held off by an unrelated collection-time marker or an `except OSError` arm is not in the number — the line says so next to it (#1274).

**Git shims that stop shimming.** `tests/test_status_swallowed_705.py` builds its fake git by matching `$1`. git accepts global flags *before* the subcommand, so `git --literal-pathspecs status` moves `status` to `$2`, the shim's test fails, and the `exec` fallthrough runs **real git**. On a test asserting a refusal that fails loudly; on a test asserting a pass it goes vacuous and silent. Filed as #1206. Match the subcommand anywhere in `"$@"`, not at `$1`.

# What CI will tell you that a local green will not

`pytest` runs with `--tb=no` (`.github/workflows/tests.yml`), so **no traceback ever reaches the logs** — the `junit_summary.py` step prints the failing assertion from `junit.xml` instead. A missing traceback is the workflow's choice, not a truncated reader.

A single-platform red is usually real, not a flake: the running score is 10 genuine to 2 flakes, and the platform you write on is the one that cannot see its own constraint. Read the log before re-running; a re-run costs the same call and destroys the evidence.

The best discriminator on a red leg is **which tests passed on it** — `2 failed, 4793 passed` tells you the product is not disarmed and the fault is in the fixtures.
