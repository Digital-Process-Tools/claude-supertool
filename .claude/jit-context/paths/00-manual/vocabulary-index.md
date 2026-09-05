---
title: "Vocabulary index: the 3rd column"
match: \.claude/jit-context/vocabulary/
---

- **`00-index.tsv` is `keyword⇥file⇥verdict`, 3 fields since `claude-jit-context` 0.7.1** (until #2211 this repo's committed index carried 2, and `01-paths.tsv` in the same directory — a different builder, module name → file — still does; this column only touches `00-index.tsv`). `verdict` is empty or the literal word `generic`: `pre-prompt-hook.sh`'s deferred generic-word classifier (#232/#255) marks a keyword on the project's generic-word list `generic`, and a file matched only through `generic` keywords is downgraded to its summary rather than shown in full — a missing/empty verdict degrades to specific, same as an index built before this column existed.
- **`keyword` is never handed to awk** — matched with a literal `index()` against a padded prompt (`pre-prompt-hook.sh:266`), unlike a tools `match` or a paths `pattern`, so dead-escape/case findings never apply to it.
- **`jit-index` reads the 3-field shape only under a `vocabulary/` layer, never by shape alone** (#2211 review) — a hand-truncated tools row missing its last three columns collapses to the identical 3 fields, and unlike a vocabulary keyword a tools `match` column is supposed to be checked, so the validator asks which directory a file is in before it asks what a row looks like.
- **`tests/test_jit_vocabulary_index_three_columns_2211.py` pins `vocabulary/00-manual/`'s count. `vocabulary/01-oss/` is deliberately left out** — `/oss:scaffold`-owned and replaced wholesale, so pinning it here would fight that tool rather than `rebuild-tsv.sh`.
