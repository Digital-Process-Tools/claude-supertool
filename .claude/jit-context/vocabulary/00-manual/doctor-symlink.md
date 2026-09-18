---
title: "_doctor_symlink -- cwd-exclusion is right for the spawn, wrong for the display"
keywords: _doctor_symlink
---

`_doctor_symlink()` (`_supertool.py`) resolves `supertool` on `PATH` via
`_which_excluding_cwd("supertool")` (#2611) rather than a bare `shutil.which()` -- correct,
because it then spawns the result (`[which, "version"]`) two lines later, and a raw
`shutil.which()` would let a repo-planted shim at cwd win on Windows (#2596/#2575's class).

**Same value is also rendered to the user** as "where does `supertool` resolve on your PATH".
`_which_excluding_cwd()` deliberately skips a `PATH` entry equal to cwd -- correct for the spawn
decision, wrong for the display one. A user who legitimately `cd`s into their own install
directory (e.g. `cd ~/.local/bin`, the exact case `_doctor_symlink()`'s own docstring describes
inspecting) before running `doctor` gets `which: None` for a binary that genuinely resolves via
plain `shutil.which()` -- rendered identically to a real absence (`- not found on PATH`), no
distinguishing note.

**Open design decision, not yet made** (per the #2616 fix commit's own message, which logged
this rather than guessing): should `_doctor_symlink()` show the raw `shutil.which()` result for
DISPLAY while still spawning through the cwd-excluding path, or note the exclusion explicitly
instead of rendering as absence? Either way the fix belongs in `_doctor_symlink()`'s own
logic/rendering -- `_which_excluding_cwd()` itself is correct for every one of its other
(spawn-only) call sites (`_has_rtk()`, `_has_ctags()`, the `validators/common/spawnable.py` /
`presets/_spawnable.py` siblings) and should not be loosened to fix this one display case.
