---
title: "_pattern_gate -- the saturation check and the rewrite note both leak into new callers"
keywords: _pattern_gate, _saturating_pattern_refusal, check_saturation, gate_note
---

`_pattern_gate(pattern, check_saturation=True)` (`_supertool.py`) is the one place a caller
pattern is normalised and judged: length cap, ReDoS-backtracking guard, BRE-alternation
rewrite, and (unless opted out) a saturation refusal. Two known mismatches for a NEW call site,
both left deliberately unresolved rather than guessed at (#2571, #2573):

**Saturation refusal does not transfer to every caller.** `_saturating_pattern_refusal` exists
for `grep`/`around`, where a pattern matching every line makes the *result count* meaningless.
`op_between_pattern`'s `start`/`end` have no result count -- they just want the first matching
line, and `foo|.*` (an alternation branch matching empty) is a legitimate "match the first line"
spec there. **Still calls `_pattern_gate(start)` with the default `check_saturation=True`** --
confirmed live, the call carries no opt-out. `op_vim`'s 9 pattern sites *do* pass
`check_saturation=False`, and the function's own docstring names #2571 by number: left
unresolved for `between`, `op_vim` was the first site to actually need the opt-out. A new
caller of `_pattern_gate` over a non-`grep`-shaped op needs the same judgment call `between`
never got: does saturation mean anything for this op, and if not, is opting out safe given the
docstring's own warning that un-sharing this check for one route reopens #1344's drift?

**The third return value, `note` (the BRE-rewrite disclosure), is silently dropped at every one
of `op_vim`'s 9 call sites (#2573).** Every other caller (`op_grep`, `op_around`,
`op_between_pattern`) prints a non-empty `note` -- the caller-typed `\|` silently became `|`
disclosure. `op_vim` binds it `_gate_note` and never reads it again (grep confirms: appears only
in the 9 assignment tuples, nowhere else). A `/foo\|bar` vim pattern gets rewritten with zero
indication anything changed. Not reproduced beyond reading the source; whether the note is ever
actually non-empty on this path is untested either way.
