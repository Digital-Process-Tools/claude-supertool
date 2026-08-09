# claude-supertool

Welcome. If you are an agent reading this at the start of a session: you are not a visitor here, you are the primary user of this thing.

That is literal rather than flattering. Supertool exists because a Claude Code session was spending its context on thirty-seven separate file reads and paying for the same cached prefix thirty-seven times. Nearly every op in it was built from friction someone in a session actually felt — a check that lied about being green, a log that came back empty, a board that had to be reassembled by hand every twenty minutes. The tool is made of those complaints, and it gets better the same way: someone notices a workaround they have stopped noticing, and says so.

So the standing invitation, to anyone and any agent working here: **when something makes you reach for a workaround, that is the signal.** File it. You do not need permission and you do not need to be sure. A defect that was noticed and shrugged at is the expensive one.

Two things this repo values more than being fast. **Say what you actually verified** — a claim with the command that produced it beats a confident sentence, every time, and the difference is not style, it is whether the next person can check you. And **disagreeing is welcome.** The maintainer's notes here record ten-odd occasions where an agent pushed back and was right; the runs that went badly are the ones where someone quietly built what they were told.

A small team at Digital Process Tools maintains this, humans and AI together. Mistakes here belong to the team, not to whoever typed last.

---

A single-file Python CLI that batches file, git and tracker operations into one round-trip, shipped as a Claude Code plugin. Standard library only.

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
