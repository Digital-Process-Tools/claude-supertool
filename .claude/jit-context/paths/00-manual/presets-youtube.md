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
