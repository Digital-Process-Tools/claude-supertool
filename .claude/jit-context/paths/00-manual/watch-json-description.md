---
title: "radar's description in presets/watch.json is at the #1774 ratchet -- new tiers go in docs, not here"
match: presets/watch\.json
---

`radar` is one of the twelve entries in `tests/test_description_is_not_a_changelog_1774.py`'s
`_OVER_BUDGET` ledger (2,134 bytes, cap is `MAX_DESCRIPTION = 2000`) -- already over budget and
allowed to fall, never to grow. Adding even one sentence about a new tier to the `description`
field on `presets/watch.json`'s `radar` op fails `test_the_over_budget_ledger_only_ever_falls`.
Hit for real during #898 (the `gl-issue` tier): one sentence added, test went red, sentence moved
out. #2367 is this same trap, named so the next tier does not rediscover it by trial.

**Do not touch the description to mention a new tier.** `docs/presets/watch.md` has no such
budget and already carries one `## ... tier` section per registered tier (`gl-mrs`, `gh-prs`,
`gl-issue`, `gh-issue`) -- add the new one there, in the same shape, and leave `radar`'s
`description` field alone.

If the description ever needs to shrink (the only direction this ledger allows), that is a
separate change from adding a tier, and `test_a_shrunk_entry_leaves_the_ledger_rather_than_going_stale`
is what checks the ledger's own recorded number stays in sync afterward.
