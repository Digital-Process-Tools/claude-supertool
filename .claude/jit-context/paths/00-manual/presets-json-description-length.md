---
title: "An op description's byte length is a cross-cutting guard, not a per-file one"
match: presets/.*\.json
---

**Changing any op's `description` field in ANY `presets/*.json` -- not just the one you're
touching -- can redden three sibling guards that share no mapping to the file you edited:**

- `tests/test_meta_doc_figures_1783.py` pins two exact byte counts in `docs/operations/meta.md`
  against the live rendered size of `ops:full` minus `ops`.
- `tests/test_description_is_not_a_changelog_1774.py` is a hard ratchet: an op already in
  `_OVER_BUDGET` (already over the 2000-char cap) may only shrink, never grow.
- `tests/test_render_size_claims_1877.py` pins `~NN.NNKB` for `ops:full`'s rendered size in four
  docs files (`docs/operations/index.md`, `docs/contributing.md`, `hooks/session-start.sh`,
  `docs/operations/meta.md`).

Cost when missed (#2536): two full CI round-trips, all three guards red identically both times,
because the first fix round only checked the one guard a report named rather than sweeping every
guard the same class of change reaches. A guard-derivation lookup keyed on the touched *file*
cannot catch this class at all — it needs to be keyed on the touched *field* (any `description`
value inside any `presets/*.json`), which nothing currently derives automatically.

Before shipping a description-length change anywhere under `presets/`, run all three tests, not
just the one CI happened to name first. `presets/watch.json`'s own `radar` entry carries a narrower,
more specific version of this same trap — see `watch-json-description.md`.
