---
title: "Three states, not two -- a checker that cannot answer says so"
description: "The repo's central defect class: an absence the tool produced, rendered as an absence in the world. ok / finding / skipped, and the third never renders as the first."
keywords: three states, third state, could not run, cannot tell, skipped rather than, absence read as
---

**The defect: an absence produced by the tool, read as an absence in the world.** A grep that
truncated silently, a cancelled check leg counted as neither pass nor pending, empty stdout from a
refusal read as "zero errors". Filed more than a dozen times under different surfaces.

**The fix is always the same shape: `ok`, a finding, and a third state that is neither.**

| Surface | The three (or four) | The collapse to refuse |
| --- | --- | --- |
| validators | `ok` / finding / `skipped` | `skipped` is not `ok`. A skip renders no ✗, never triggers `rollback_on_fail`, and is never written to the cache |
| `gh-branch` | `GREEN` / `NOT GREEN` / `NO RUN` / `UNKNOWN` | `UNKNOWN` is a job list that did not come back — never counted as zero passing legs. Exit 0 only on `GREEN` |
| `git-worktrees` | `idle` / occupied / `cannot tell`; merged / `merge unknown` | `cannot tell` is not `idle`; `merge unknown` is not `merged`. Neither is permission to reap or to brief into |
| check tally | every leg named | `CANCELLED`, `SKIPPED`, `TIMED_OUT`, `NEUTRAL`, `ACTION_REQUIRED` are none of them passes and none of them pendings. The states must sum to the leg count |

`docs/validators.md` §"Declining instead of guessing" is the written contract. An absent tool
(`node`, a linter nobody installed) is `skipped`, never `ok`.

**Writing a new checker:** the three-state helper usually already exists — look before inventing
vocabulary for it. Name what went unchecked, not just that something did.

**Reading a result, which is where this fires hardest:** when an answer would let you report a
negative — no matches, no such op, never ran — get it a second way before it becomes a sentence.
A rule that never matched and a rule that never loaded log identically; so do a grep that found
nothing and a grep that was refused.

**Do not trade the loud bug for the quiet one.** Suppressing a crash, clamping a range or
defaulting a filter all look like fixes and all convert "it broke" into "it silently gave you
something else".
