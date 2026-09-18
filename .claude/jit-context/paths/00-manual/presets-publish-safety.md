---
title: "presets/_publish_safety.py -- a failed mode-bits probe reads as bits-not-enforced"
match: "presets/_publish_safety.py"
---

# `_mode_bits_are_enforced()` has two `except OSError` arms, both return `False`

`check_token_file_mode()` (#227) uses `_mode_bits_are_enforced()` to tell "this filesystem
does not enforce POSIX mode bits" (soft `WARNING`, could-not-verify) apart from "this filesystem
enforces them and the file is loose" (hard `ERROR`, exit 2). The function creates a `tempfile`,
`chmod`s it `0o600`, and re-`stat`s it -- but **both** `except OSError` arms (probe creation
failing, e.g. unwritable/full `TMPDIR`; and the chmod/stat step failing) return the same `False`
as a clean measurement of "not enforced".

**Open, unfixed** (`trap.d/2575.mode-bits-probe-failure-conflated-with-unenforced.md`, release
audit round 2, carried past the round cap on purpose): a probe that could not even run and a
probe that ran and measured "not enforced" render identically. A loose-mode credential file then
takes the soft `WARNING` arm on a POSIX box under a sentence that blames Windows semantics for a
fact that was never measured -- the one case this function exists to catch.

Fix shape if you touch this: give `_mode_bits_are_enforced()` a third return (could-not-tell) for
probe-creation failure, distinct from a measured "not enforced", and make `check_token_file_mode`
treat that as its own case rather than folding it into the soft warning -- the general shape is
`three-states.md`, applied to this one function.
