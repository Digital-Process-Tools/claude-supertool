---
title: "The 01-oss layer is /oss:scaffold's, and one file of it is deliberately untracked"
match: \.claude/jit-context/(paths|tools|vocabulary)/01-oss/
---

`/oss:scaffold --apply` replaces this whole layer wholesale on every run, so an edit here is lost at the next one. Two consequences, and the second is the one that costs time.

**`tools/01-oss/supertool-required.md` is written on disk and NOT tracked here, on purpose.** It is the plugin's own `PreToolUse` block naming `Read`/`Edit`/`Write`/`Glob`/`Grep` — the same job `.claude/jit-context/tools/00-manual/harness-tools-blocked.md` already does locally (which is why that one sits in `hooks/shipped_rules.NOT_SHIPPED`). Its text has already been read twice by a spawned reviewer as a fabricated prompt-injection payload rather than a first-party tool constraint (#1793). A second tracked copy buys nothing and reproduces the read that goes wrong.

So after any `/oss:scaffold --apply`: `git rm` it and clear its row from `tools/01-oss/00-index.tsv` again. **The row and the file move together**, or the row points at nothing. `tools/01-oss/00-index.tsv` is committed empty and that is the correct state.

`tests/test_harness_tools_do_not_ship_1791.py::test_the_rule_the_issue_describes_is_not_in_this_tree` asserts this against `git ls-files`, not a filesystem walk, precisely so the untracked copy scaffold writes does not redden it.
