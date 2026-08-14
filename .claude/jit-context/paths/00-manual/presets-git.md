---
title: "presets/git/ — every splitlines() here is judged, not swept"
match: "presets/git/"
---

**Register test — first gate a new site hits.** A new `x.splitlines()` under
`presets/git/` is RED until judged — `tests/test_preset_git_splitlines_register_1130.py`,
keyed `path::enclosing function`, 24 entries / 27 call sites in 9 files,
AST-verified (grep also hits docstring mentions of `splitlines()` as text —
don't trust it here). Add a site via
`REGISTER["presets/git/foo.py::func"] = "GROUND - reason"` (>40 chars), or use
`_untrusted.split_lines` instead (absent from the register by construction).

**The deciding rule is NOT "does the reader disable quoting". That rule was
published here for a year and answered for 3 of 27 entries (#1654).**
`core.quotePath` quotes **pathnames** and nothing else. Measured on git 2.46.2:
`ls-files` octal-quotes a non-ASCII name, while `log --format=%s`,
`for-each-ref`, `branch -vv`, `show` content and `blame --line-porcelain` all
emit a U+2028 raw — and stderr is never a pathname at all. Only `git-diff`
turns quoting off (`diff.py:203,233`), which is true and is not the question.

Ask three, in order, and open the register entry with the ground you land on:

1. **Does the site read a pathname, and only a pathname?** → `QUOTED PATH -`.
   Six entries: `_git_common::_list_conflicts`, `push::_uncommitted_leftovers`,
   `push::_recover_by_rebase`, and `checkout/diverge/status::main`, each down to
   one `--porcelain` / `--name-status` read since #1681.
2. **Otherwise, name what makes the harm survivable** → `NOT QUOTED, harmless -`.
   Fail-safe (a forged row can only refuse), fail-closed, inflates a count in
   its loud direction, or the writer is the local operator. 17 entries.
3. **Neither?** → narrow to `_untrusted.split_lines`, and `_untrusted.flat` the
   segment if a selection (`[0]`, `[-1]`, first-non-empty) renders it. Splitting
   alone answers forgery and not loss: consuming the separator discards the
   other half, so the writer still picks the segment (#1652 retired the
   opposite claim; `test_no_entry_rests_on_the_retired_reasoning` keeps it out).
4. **Does it render EVERY line, counted?** → `split_lines` **and**
   `visible()` per line — never the split alone, which fixes the count and
   leaves the separator live in a row the tool owns (#1681, eleven sites in
   `checkout/diverge/push/status/trail`). Arm 2 answers "is a forged row
   survivable" and at these sites the count IS the product, so a correct arm-2
   entry is still the wrong answer. `keep="\t"` where the line is parsed on a
   TAB field or is indented source.

**`resolve.py` — 9 sites, deliberately unchanged.** (Its three child-stream
relays into the receipt were a different question and are `_untrusted.flat`-ed
since #1638; `cannot read/write: {e}` next to them is NOT, because
`str(OSError)` reprs the filename.) Conflict-marker state
machines split file content, key on `<<<<<<<` at column 0. A forged separator
grants nothing a plain newline doesn't already grant. `_scan_markers`/
`_count_blocks` can only over-report (fail-safe, just refuses a stage);
`_union_attr_paths` is fail-closed.

**Registered risk, not yet fixed:** `investigate.py::main` (~103) — blame
`--line-porcelain` parse keys on file CONTENT → a crafted line can misattribute
author/date. The only `NOT QUOTED, open` entry. `worktrees.py::remote_branch_names`
was the second and is CLOSED (#1654): `check-ref-format` accepts U+2028, so one
published ref became two records and an unpushed branch read as pushed.

**Op traps:** `git-push` refuses an ambiguous upstream rather than guessing —
use `git-push:set-upstream`. `git-resolve` leaves a branch conflicted on
refusal, doesn't skip it.
