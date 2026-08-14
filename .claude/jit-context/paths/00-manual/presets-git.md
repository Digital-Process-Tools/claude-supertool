---
title: "presets/git/ — every splitlines() here is judged, not swept"
match: "presets/git/"
---

**Register test — first gate a new site hits.** A new `x.splitlines()` under
`presets/git/` is RED until judged — `tests/test_preset_git_splitlines_register_1130.py`,
keyed `path::enclosing function`, 29 entries / 41 call sites. Add a site via
`REGISTER["presets/git/foo.py::func"] = "reason"` (>40 chars), or use
`_untrusted.split_lines` instead (absent from register by construction).
Current: 41 sites / 11 files, AST-verified (grep also hits docstring mentions
of `splitlines()` as text — don't trust it here). Pre-#1130-fix: 44 / 12
files; `diff.py` dropped to 0 native calls.

**Deciding rule for a new site: does the reader disable quoting?**
`core.quotePath` defaults ON — git octal-quotes bytes above 0x7F (incl.
U+2028) before a split sees them, quoting absorbs the separator for free.
**Only `git-diff` runs `-c core.quotepath=false`** (`diff.py:203,233`); every
other reader here leaves quoting on. Test: does the site's `_git([...])` call
pass `core.quotepath=false`? Yes → narrow to `_untrusted.split_lines`. No →
quoting already does the job, leave `.splitlines()` alone.

**`resolve.py` — 9 sites, deliberately unchanged.** Conflict-marker state
machines split file content, key on `<<<<<<<` at column 0. A forged separator
grants nothing a plain newline doesn't already grant. `_scan_markers`/
`_count_blocks` can only over-report (fail-safe, just refuses a stage);
`_union_attr_paths` is fail-closed.

**Registered risks, not yet fixed:**
- `investigate.py::main` (~103) — blame `--line-porcelain` parse keys on file
  CONTENT → a crafted line can misattribute author/date.
- `worktrees.py::remote_branch_names` (~647) — `for-each-ref` into a
  pushed/unpushed set; `check-ref-format` ACCEPTS U+2028 (#1119) → a hostile
  remote ref can spell your branch name in its tail, unpushed reads as pushed.

**Dead code:** `status.py:459-464` assigns `current_branch`/`tracking` from
`branch -vv` — never read; render uses `rev-parse --abbrev-ref HEAD` at `:467`.

**Op traps:** `git-push` refuses an ambiguous upstream rather than guessing —
use `git-push:set-upstream`. `git-resolve` leaves a branch conflicted on
refusal, doesn't skip it.
