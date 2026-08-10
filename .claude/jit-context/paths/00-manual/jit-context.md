---
title: "Writing a jit rule"
match: .claude/jit-context/
mode: remind
---

- **The `.md` is inert.** `00-index.tsv` in the same directory is what the hook reads. Tools: `tool⇥match⇥file⇥mode⇥keyword⇥` (6 fields, trailing tab). Paths: `prefix⇥file` (2 fields). No row = a rule that never runs, which reads exactly like a rule that never matches.
- **Verify by running the forbidden command.** Nothing else distinguishes the two. The hook logs to `.discovery/logs/hooks.log`; a non-firing rule shows `(none) [shown:0]` on the very command it was written for.
- **Keep it short.** Injected in full on every match — length is a cost paid forever. Table of replacement ops, the measurement, stop.
- **Point at the op, never at a way around it.** If the entry explains how to get the answer by hand, fix the op instead.

Two `match` traps, measured 2026-08-10:

- **awk, not PCRE.** macOS ships one-true-awk (`match(tolower(full_command), ...)`, `pre-tool-hook.sh:80`), which drops `\s`/`\d`/`\w` as undefined escapes — `gh\s+pr` compiles to `ghs+pr` and matches nothing. Two of six tool rules were dead this way, both `block`. Use `[[:space:]]`. `\n` is a valid escape and does survive.
- **`^` anchors the whole command, not each line**, and the match runs against the entire string — so anchor `(^|[;&|\n] *)`, or a rule misses line 3 of a heredoc and fires on a mere mention of the command inside a payload.
