---
title: "tests/_go_warmup_lock.py took four independently-correct fixes in one day"
match: tests/_go_warmup_lock\.py
---

`serialize_once` and `_lock_dir_usable` in this file took four separate, independently-landed,
individually-correct fixes in one calendar day (2026-09-16: #2552, #2555, #2559, plus #2548 on the
poller beside it) — each one closing a real bug, and each one uncovering the next rather than the
file settling. #2559 was a residual left by #2555 (a retry loop that fixed a real race but paid the
full timeout on a genuinely-unusable `lock_dir`), and #2559's own merge immediately surfaced a
further residual (#2563: the write-probe's own fail-open path on a transient failure).

**Before touching this file again, read the whole function fresh rather than just the diff's own
hunk.** A shared, low-level concurrency primitive (locking, retry, serialization) that has taken
this many same-day landings is a signal that each fix so far has been locally correct and
non-locally incomplete — the interaction between failure branches is where the next bug lives, not
inside any one branch reviewed alone.
