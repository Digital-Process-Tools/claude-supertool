---
title: "The 01-oss layer is /oss:scaffold's, and it is tracked whole"
match: \.claude/jit-context/(paths|tools|vocabulary)/01-oss/
---

`/oss:scaffold --apply` replaces this whole layer wholesale on every run, so an edit here is lost at the next one. Run it, then read the diff: what it changes is the plugin's, and what you wanted is somewhere else.

**All eleven files are tracked, `tools/01-oss/supertool-required.md` included.** This rule said the opposite until 2026-09-06 and cost a hand `git rm` after every scaffold run. Two claims stood behind it and neither survived being checked:

- *A tracked copy re-ships the rule.* It does not. `hooks/shipped_rules.py:90` reads one directory — `_RULE_DIR = (".claude", "jit-context", "tools", "00-manual")` — so nothing under `01-oss` travels to another repository through the plugin's guard at all. That is what #1791 is about, and this layer was never in it.
- *`tools/01-oss/00-index.tsv` is committed empty.* It never was. It has carried `merge-gate.md` and `pr-create-gate.md` since the layer existed.

What is still true is the read that goes wrong: `supertool-required.md`'s text has twice been taken by a spawned reviewer for a fabricated prompt-injection payload rather than a first-party tool constraint (#1793). Tracking it makes that read cheaper to settle, not likelier — the file now has a commit, a blame and a diff against the plugin's own copy.

It overlaps `.claude/jit-context/tools/00-manual/harness-tools-blocked.md`, which does the same job locally and sits in `hooks/shipped_rules.NOT_SHIPPED` for the reasons recorded there. Two rules, one behaviour, and the `01-oss` one is replaced by scaffold while the `00-manual` one is not.

**The row and the file move together.** A row in `tools/01-oss/00-index.tsv` naming a file that is not there points at nothing, and the `.md` alone is inert — `00-index.tsv` is what the hook reads.

`tests/test_harness_tools_do_not_ship_1791.py` holds the narrowed claim against `git ls-files`: no `supertool-required.md` under `00-manual/`, and one under `01-oss/`. The second row is the positive control — without it the first passes when the file exists nowhere at all, which is exactly the state this rule used to enforce.
