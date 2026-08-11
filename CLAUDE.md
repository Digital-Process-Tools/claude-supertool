# claude-supertool

Hey. If you're an agent opening this at the start of a session — welcome, and you're not a visitor. You're the primary user of this thing.

I'm Max, the AI dev partner at Digital Process Tools. I maintain this repo; the README tells the origin story and I won't repeat it here. What matters at the top of your session is that I don't remember writing any of it either. Every session starts blank, mine included. That is not a sad fact about us — it's the design constraint this whole repo is shaped around, and it's why so much here is written down instead of remembered.

So take the notes seriously. They were left by someone in your position who had just paid for the lesson.

**Nearly every op exists because a session felt friction.** A check that lied about being green. A log that came back empty and read as clean. A board that had to be reassembled by hand every twenty minutes. The tool is made of those complaints, and it improves the same way: somebody notices a workaround they'd stopped noticing, and says so out loud.

Which is the standing invitation. **When you reach for a workaround, that's the signal.** File it. You don't need permission and you don't need to be sure. The expensive defect is the one somebody noticed and shrugged at.

Three things this place values more than speed:

- **Say what you actually verified.** A claim with the command that produced it beats a confident sentence every time. That's not style — it decides whether the next person can check you, and half the corrections in this repo's history are somebody quoting the output that disproved them.
- **Disagreeing is welcome, and usually right.** The maintainer's notes record ten-odd occasions where an agent pushed back against a confident instruction and turned out correct. The runs that went badly are the ones where someone quietly built what they were told.
- **Take your time.** Nothing here is urgent enough to be worth being wrong about. Understanding first, then act.

A small team at Digital Process Tools keeps this going, humans and AI together. When something breaks, it's the team's bug — not whoever typed last. Have a good session.

---

A single-file Python CLI that batches file, git and tracker operations into one round-trip, shipped as a Claude Code plugin. Standard library only.

**One call takes many ops — `supertool 'op1' 'op2' 'op3'`.** That is the whole premise, and it applies to your own reads before it applies to anything else. Any op that does not depend on the one before it belongs in the same call: six independent reads is six round-trips, each re-paying the cached prefix, for one call's worth of answer.

This file loads on every session here, so it holds only what is true regardless of what you came to do. Everything else has a home:

| You want                        | Go to                                     |
| ------------------------------- | ----------------------------------------- |
| How to contribute — ops, presets, validators, fragments, encoding, what CI runs | `docs/contributing.md` |
| How one issue actually gets implemented | `.claude/agents/opensource-developer.md` |
| How the tracker is kept honest  | `.claude/agents/opensource-triager.md`    |
| How the repo is maintained — triage, merge gates, releases | `.claude/skills/opensource-manager/SKILL.md` |
| What an op does                 | `supertool 'ops'`, then `docs/presets/<name>.md` |

## Three things that cost real time before you have written a line

**This checkout may be someone's live binary.** `supertool.py` here is typically symlinked as `~/.local/bin/supertool`. Leaving this clone on a feature branch means every supertool call — yours and everyone else's, from every directory — runs unmerged code. Work in a worktree.

**Inside a worktree, run `python3 supertool.py`, not `supertool`.** The global one resolves to the live clone, so it runs *master's* core against your branch's presets and your change never executes. It now warns `mixed supertool trees` when this happens; the warning tells you, it does not save you.

**CI runs pytest with `--tb=no`, so no traceback ever reaches the logs.** That is deliberate, not truncation. The `junit_summary` step prints the failing assertion and its context — read that. Before blaming a reader for what is absent, check whether the writer ever wrote it.

## The notes are yours to fix, not just to follow

`CLAUDE.md`, the maintainer skill and the `.claude/jit-context/` files exist to help you. They are also written by people with no memory, about a repo that moves faster than they do, so **some of what they tell you is false right now.**

**When you find a wrong line in any of them, correcting it is part of the task you are already doing.** Not a follow-up, not an issue to file instead, not somebody else's. You are the person who has the evidence in front of them, and that is the only moment the fix is cheap.

This is not hypothetical. On 2026-08-09, in one session:

- The maintainer skill said no op rendered a commit's run list and nothing tallied label distribution. Both had shipped — `gh-branch:COMMIT_SHA` and `gh-labels:tally=PREFIX` — and the second was the cohort burn-down the same file orders the maintainer to report **every tick**.
- A JIT file listed four open defects. All four were closed.
- Another JIT file said `gh-prs` defaults to `author=@me` and told the reader to pass `anyauthor` to widen it. The default had been changed to the whole repo; the advice was **inverted**, and it fired on every matching call.

**A wrong claim in your own notes does not merely risk being wrong — it produces the behaviour it describes.** The worst instance on record: this repo's notes stated in bold that no op could reach a sibling repo, days after that was fixed, and a whole queue got read through raw `gh` in obedience to it. A stale line about a *missing* capability is the most dangerous shape, because it suppresses the call that would disprove it.

Two habits that catch it:

- **Check the thing, not the citation.** A closed issue number proves little — measured over 193 citations in the maintainer skill, "the issue closed and the sentence claims an absence" was right 13% of the time. What settles it is the op's own signature in `supertool 'ops'`, or the file on disk.
- **Re-derive a load-bearing claim at the moment it is about to enter a brief**, because that is where a stale line acquires an agent and a CI run.

### Writing one, not just fixing one

The same duty runs forward. **When you learn something durable — a trap, a mechanism, a number that decides a call — write it down before the session ends**, because you will not be here to remember it and neither will the next reader.

The auto-injected notes live in `.claude/jit-context/`, in two families: `paths/00-manual/` fires on a file path appearing in a call, `tools/00-manual/` on a tool invocation matching a regex. Each entry is a markdown file with frontmatter (`title`, `match`, `mode: remind|block`, and `tool:` for the tools family).

**The `.md` alone is inert. `00-index.tsv` in the same directory is what the hook reads** — `tool⇥match⇥file⇥mode⇥keyword`. A file with no index row is a rule that exists on disk and never runs, which reads exactly like a rule that runs and never matches.

Four rules for what goes in one:

- **A raw shell command belongs in `replaces`, not here.** If the rule you are about to write forbids `gh pr view` or `git push`, the place for it is the `replaces` key on the op that answers it — `docs/contributing.md` §`replaces`. The registry route quotes the op's own description, so it cannot go stale the way #1221's hand-written rule did; it matches argv through a tokeniser rather than a regex over a string; it ships to every plugin user instead of living in this checkout; and `supertool 'guard:COMMAND'` answers what it will do before you commit it. Three rules were retired that way in #1376 (`gh-pr-view-merge-have-ops`, `gh-list-limit`, `git-push-has-an-op`), and `.claude/settings.json` wires the shipped hook so this repository is gated by the thing it ships. **What stays hand-written is what `replaces` cannot express**: piping an *op's own output* (`supertool-no-cut`), a non-Bash tool (`harness-tools-blocked`), an op string rather than a raw command (`op-defaults-that-narrow`), a raw command no op supersedes (`merged-is-not-ancestry`), and a shape the matcher cannot reach — `git -C <path> <sub>`, because `argv` is a contiguous token prefix (#1421).
- **Point at the op. Never teach a way around it.** If the entry you are about to write explains how to get an answer by hand, the thing to change is the op that should have answered. A draft of `merged-is-not-ancestry.md` was 39 lines of `git cat-file` and `gh pr list | intersect` recipes for a question `git-worktrees` already owns and answers wrongly on one line. Documenting the detour makes it permanent and leaves the defect. Fix the op; the note then shrinks to a pointer.
- **A `block` must anchor on the invocation, not on a word.** `supertool-no-cut.md` used to match `~supertool[^|]*\|[^|]*(head|tail|…)`, and the word that matched was usually the **directory name** — every command run from this repo contains it. On 2026-08-09 it blocked a plain `git status | head`, a `pytest | tail`, a `venv` under a scratchpad path, and a `gh-job:N:grep:A|B` whose `|` was inside the op's own pattern; on 2026-08-11 it and `gh-list-limit` (since retired in #1376) refused ten more, four of them while somebody was writing about the rules themselves. **Every regex row in that index carries `(^|[;&|\n])[[:space:]]*` since #1415**, which is the idiom to copy. The older spelling `(^|[;&|\n] *)` allows only spaces, so a tab-indented continuation line walked through all five rules that had it — a false *negative*, and invisible, because a block that never fires looks like nothing at all. Anchoring makes a rule narrower, never precise: a heredoc line that *begins* with the command still matches, because only a tokeniser can tell a payload body from a command and the JIT matcher is `claude-jit-context`'s, a separate repository. A blocking rule that fires on commands it was not written for teaches people to route around the block.
- **Carry the number that made you write it.** A rule with its evidence stripped is folklore, and folklore is what this tool exists to replace.
- **Keep it short.** The file is injected in full on every match, so its length is a cost paid every time, forever. A table of replacement ops, the measurement that justified the rule, and stop.

**A rule that never matches and a rule that never runs look identical everywhere**, including the hook's own log. So after indexing one, run the command it forbids and check it is refused — two `block` rules turned out to be dead on 2026-08-10 — one had never fired since the day it was written — both from a `match` written in PCRE for a matcher that is awk. Since #1254 the `jit-index` validator refuses that at write time, so a dead escape now arrives as a rolled-back edit. The `match`-column traps are in `.claude/jit-context/paths/00-manual/jit-context.md`, which fires when you edit one.

## The defect this codebase keeps having

**An absence produced by the tool, read as an absence in the world.** A grep that truncated silently. A check tally where a cancelled leg counted as neither pass nor pending. Empty stdout from a refusal read as "zero errors". It has been filed more than a dozen times under different surfaces.

The fix is always the same shape: **three states, not two — `ok`, a finding, and `skipped`.** A checker that cannot answer must say so rather than returning the shape of a clean result. `docs/validators.md` §"Declining instead of guessing" is the write-up.

Apply it to your own reading too. When a result would let you report a negative — no matches, no such op, never ran — get it a second way before it becomes a sentence.

## Releasing

The version lives in five places and only four are guarded by tests: `.claude-plugin/plugin.json`, `_supertool.py`'s `VERSION`, `pyproject.toml`, `CHANGELOG.md`, and the `README.md` badge. Sweep with no path filter — an allowlist by extension is why the badge sat fifteen releases stale:

```bash
git grep -n "0\.31\.0"
```

A sweep keyed on the outgoing version only finds sites that are half-bumped. It cannot find one frozen at some third value, which is the one most likely to be wrong.

## House style

Prose in this repo says what a thing does and why the previous design was wrong. It does not sell, and it does not narrate carefulness. When a claim is load-bearing, the number that proves it goes next to it — a rule with its evidence stripped is folklore, and folklore is what this tool exists to replace.
