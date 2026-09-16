---
title: "The session scratchpad is shared across concurrently running lanes"
match: /scratchpad/
---

**Never a bare fixed filename under the shared scratchpad — every intermediate file, not just the
note/report/PR-payload trio the developer brief already names.** A generic name
(`.../scratchpad/note.toml`) can be overwritten between your own `write` and a later `read` by a
concurrently running sibling lane's write to the exact same path.

Observed live 2026-09-04 (#2015): a payload written at a generic scratchpad filename came back, on
re-read, as a different lane's note content (`fix/2169`, an unrelated worktree, running on the same
machine at dispatch time) — not corruption, a same-content coincidence, but the mechanism that
produced it is real. Whether the scratchpad path (which looks per-session, carrying what reads as a
session UUID) is genuinely shared across sibling lanes, or something else produced the same observed
effect, was not settled from inside one lane.

**Discriminate every scratchpad filename by branch and timestamp** —
`fix-2015-20260904T144845Z-note.toml`, not `note.toml` — and verify content (`md5`/`head`) right
before and right after a write that another process could race.
