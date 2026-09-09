---
title: "One `channel.ts` per session — several is normal, not stale"
tool: Bash
match: ~channel\.ts
mode: once,remind
---

**N `bun … channel.ts` processes means N open sessions, not N leaks.** Each Claude session spawns its own consumer, and one carrying an older plugin version (`…/supertool/0.57.0/…` beside `0.58.0/…`) is a session opened before the last update, still working. Measured 2026-09-09: 4 consumers, 4 live sessions, delivery `FORWARDING`, one of them 0.57.0 from three days earlier.

Reported as "3 stale pollers requiring reload" on 2026-09-09 at 16:56 and again as "4 consumers, stale" at 17:18. Both wrong, same shape twice in one day.

What actually settles it, and neither is `ps`:

| Question | Ask |
| --- | --- |
| is this channel delivering | `channel:health` — names the socket-holder pid and verifies it |
| did an event reach a session | `channel:probe`, then look for the `<channel …>` tag in **your own** session. `forwarded` is not a receipt |

A consumer bound to a socket nobody reads is not a defect either: the session that spawned it decides that. Kill one only per-PID, after a named session is known dead — same rule the pollers carry in `presets/watch/`.
