---
title: "labels.lane_patterns: a shared helper in a lane invents its own findings"
match: (^|/)\.oss\.json$
---

`labels.lane_patterns` maps a lane label to the paths it owns. Two checks in the `oss` plugin read
it and **neither can run until it is declared** -- `lane_pattern_coverage` (overlap, dead globs) and
`lane_coupling` (tests spanning lanes). Undeclared is the third state, not a pass.

**Never put `presets/_*.py` in a lane.** They are shared helpers every preset may use (`CLAUDE.md`,
Layout). A lane that claims one makes every test exercising that helper span lanes *by
construction*, and the coupling check then reports a defect the config invented. Measured
2026-09-08: `_untrusted.py`, `_publish_safety.py` and `_secrets.py` in `lane-containment` produced
3 of 26 spans; removing them dropped 2 outright and took the count to 24 (#2448). `lane-containment`
is `hooks/` and nothing else.

**The coupling check matches path strings statically**, so it cannot tell an import from a filename
used as data. Four of this repo's spans were a fake path inside a mock review thread, an argument to
a `file not found` assertion, a fixture written into `tmp_path`, and a **negative control** -- the
path that must *not* trigger a validator. All four are `labels.lane_coupling_allowlist` material.

**Ten more were one architectural fact**: `presets/watch/tiers/{gh_prs,gl_mrs}.py` wrap
`presets/{github/prs,gitlab/mrs}.py`. That seam is what radar *is*, and those tests are the correct
response to it.

**Allowlist what you read, not what is noisy.** The one span left unacknowledged here is
`test_git_no_optional_locks_other_sites_1945.py` -- `dashboard.py` and `pr_merge.py` each carry a
private `_git()` instead of the shared chokepoint (#2447). A `WARN` naming one real thing is worth
more than a clean run.

Run either by hand while editing, with the plugin's `scripts/` on `PYTHONPATH`:
`lane_pattern_coverage.lane_pattern_report(repo, patterns)` and
`lane_coupling.lane_coupling_report(repo, patterns, allowlist=...)`.

`uncovered_count` gates nothing and is not a score. It moved 336 -> 152 on a change that should have
raised it; unexplained, and recorded here so a lower number is not read as an improvement.
