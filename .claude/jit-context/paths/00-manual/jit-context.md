---
title: "Writing a jit rule"
match: .claude/jit-context/
mode: remind
---

- **The `.md` is inert.** `00-index.tsv` in the same directory is what the hook reads. Tools: `tool⇥match⇥file⇥mode⇥keyword⇥` (6 fields, trailing tab); column 2 is a regex only when it opens with `~`, otherwise a lowercased substring. Paths: `pattern⇥file` (2 fields); column 1 is **always** a regex (`match()`, `pre-path-hook.sh:105`) — not a literal prefix. No row = a rule that never runs, which reads exactly like a rule that never matches.
- **Verify by running the forbidden command.** Nothing else distinguishes the two. The hook logs to `.discovery/logs/hooks.log`; a non-firing rule shows `(none) [shown:0]` on the very command it was written for.
- **Keep it short.** Injected in full on every match — length is a cost paid forever, and on a `block` it is the price of every *wrong* block too. Table of replacement ops, the measurement, stop. An indexed `tools/` rule over 3,200 bytes is now red (`tests/test_jit_rule_body_budget_1433.py`, #1433 — one 3,884-byte body was reported as the largest single input cost of an agent's run).
- **Point at the op, never at a way around it.** If the entry explains how to get the answer by hand, fix the op instead.

Three `match` traps, measured 2026-08-10. The `jit-index` validator now refuses the first two at write time (#1254), so you should meet them as a rolled-back edit rather than as a rule that never fires:

- **awk, not PCRE.** macOS ships one-true-awk (`match(tolower(full_command), ...)`, `pre-tool-hook.sh:80`), which drops `\s`/`\d`/`\w` as undefined escapes — `gh\s+pr` compiles to `ghs+pr` and matches nothing. Two `block` rules were dead this way, one never fired at all. Use `[[:space:]]`. `\n` survives; `\b` is a backspace, not a word boundary.
- **The tools subject is lowercased before matching**, so an uppercase literal in a `~` pattern can never match.
- **`^` anchors the whole command, not each line**, and the match runs against the entire string — so anchor `(^|[;&|\n])[[:space:]]*`, or a rule misses line 3 of a heredoc and fires on a mere mention of the command inside a payload. **`[[:space:]]*`, not ` *`** — the older spelling admits no tab, so a tab-indented continuation line walked through all five rules carrying it until #1415. Anchoring is narrower, never precise: a payload line that *begins* with a command-shaped string still matches, and only a tokeniser can tell a body from a command — and the tab widening grows that residue exactly as much as it grows the protection, since a payload line indented with **spaces** already matched. Symmetric, not new. Not checkable at write time: verify by running the command the rule forbids, **and one it must still allow** — widening a match shows up only as a wrong block.
