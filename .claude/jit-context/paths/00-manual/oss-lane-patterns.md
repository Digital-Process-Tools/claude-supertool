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

**Allowlist what you read, not what is noisy.** For a year the one span left unacknowledged here
was `test_git_no_optional_locks_other_sites_1945.py`, because `dashboard.py` and `pr_merge.py` each
carried a private `_git()` instead of the shared chokepoint. That is fixed: #2447 moved the
invocation to `presets/_git_run.py` and both files import it.

**And the span count went UP, from 1 to 2, which is the part worth keeping.** A test whose job is to
assert that two files in different lanes route through one shared chokepoint has to name both files.
So the two guards that do it -- `test_git_invocation_chokepoint_2447.py` and the rewritten
`test_git_no_optional_locks_other_sites_1945.py` -- are allowlisted, on the same ground as the ten
radar-tier spans above: an architectural fact, and the test is the correct response to it. Chasing
the number to zero here would have meant deleting the guard that keeps a fourth private wrapper from
appearing.

A `WARN` naming one real thing is still worth more than a clean run. What changed is which of the
two this particular span was.

Run either by hand while editing, with the plugin's `scripts/` on `PYTHONPATH`:
`lane_pattern_coverage.lane_pattern_report(repo, patterns)` and
`lane_coupling.lane_coupling_report(repo, patterns, allowlist=...)`.

`uncovered_count` gates nothing and is not a score. It moved 336 -> 152 on a change that should have
raised it; unexplained, and recorded here so a lower number is not read as an improvement.
