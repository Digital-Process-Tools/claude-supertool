---
title: "presets/youtube/ -- OAuth loopback bind race, Windows edge left open"
match: "presets/youtube/"
---

# `_oauth.py:authorize()` -- the loopback bind race

`_free_loopback_port()` binds port 0, closes the socket, returns the number; `authorize()`
re-binds `http.server.HTTPServer(("127.0.0.1", port), ...)` afterward. The window between the
two lets another local process take the port. Fixed for POSIX (#227): `except OSError` around
the bind now reports the race as an `OAuthError` naming `youtube_auth`, rather than a raw
traceback.

**Not settled for Windows** (#227 follow-on, reasoned not observed -- no windows-latest runner
available at the time). `HTTPServer` sets `allow_reuse_address = 1`. On POSIX that only lets a
bind reuse a `TIME_WAIT` socket, so the fixed `except OSError` still catches a live contender.
On Windows, `SO_REUSEADDR` has different documented semantics: it can let a bind *silently
succeed* against a port another active listener already holds, with no `OSError` raised at all --
so the `except OSError` arm never fires, and two servers could contend for the callback on the
same port. `test_authorize_reports_a_loopback_port_race_as_oautherror` stubs `HTTPServer` itself
rather than a real socket, so CI's windows-latest leg does not exercise this.

Not a security hole either way -- a hijacker on the loopback port still cannot pass the `state`
check at `authorize()`'s callback-validation step. What would settle it: a real-socket
double-bind test on an actual windows-latest runner. Until then this is `reasoned`, not
`observed` -- state it that way rather than as a closed gap.

# Near-simultaneous PRs in this directory left conventions half-applied

`#2593` (`reply.py`, `status_since.py` -- both new files), `#2599` (fixed `comment.py`/`auth.py`)
and `#2600` (fixed `comment.py`'s `|`-vs-`:::` parsing) landed close together against this same
directory. Three separate release audits over the composed range (not any one PR's own diff)
found conventions that one sibling file picked up and another, landing around the same time, did
not -- the same pattern three times over. **Fixed by #2649** (2026-09-19): `status_since.py`'s
`flat_hits` join now flattens `\r` as well as `\n`; `reply.py::parse_args` now offers the same
`:::` separator `comment.py` does, and its docstring's "mirrors `comment.parse_args`" claim is now
true; and every raw `sys.stderr.write(f"ERROR: {e}\n")`-shaped site across `reply.py`, `like.py`,
`list.py`, `search.py`, `status_since.py` and `read.py` (including `read.py`'s `e.message`-based
`comments_note`) now goes through `repr(safe_short(str(e), 300))`, matching `comment.py`/`auth.py`.

None of these were `forges` under this repo's existing classification -- the bodies are Google's or
the operator's own text, not a stranger's -- so each was `misreports`, non-blocking on its own.
**The lesson is not any one of the three bugs; it is that a convention established in one file of
this directory during a busy landing window is not evidence it reached its siblings.** Before
treating a just-established convention here as complete, grep every file in this directory for the
old pattern, not just the one PR's own diff.
