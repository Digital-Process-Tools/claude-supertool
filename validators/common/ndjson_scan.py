"""Locate a JSON-RPC response anywhere in an MCP daemon's stream (#1924).

The four warm adapters (`phpunit-mcp`, `phpstan-mcp`, `phpmd-mcp`, `rector-mcp`)
speak line-delimited JSON-RPC over a Unix socket to a daemon that also proxies a
forked PHP process's stdout onto the same stream. When that process fatals, the
host application can render an HTML error page into the stream, and the real
response then arrives **glued to the end of the last HTML line with no
separator**:

    </html>{"jsonrpc":"2.0","id":2,"result":{...}}

A parser that requires a JSON object to *start* a line never sees it: `</html>{"jsonrpc"...`
fails `json.loads`, the line is skipped, and the caller blocks on `recv()` until
the full call timeout — even though the answer was in the buffer from the first
`recv()` that carried it. Measured (#1924): 45614 bytes containing the answer
arrived 2 seconds into the call; the line-anchored adapter reported `NOT CHECKED`
5 minutes later.

`find_response` scans the whole accumulated buffer instead of one line at a time,
so the response is found the moment its bytes are in `buf`, wherever in the
buffer they land.

**What the scan can and cannot tell apart.** It anchors on the literal substring
`"jsonrpc"` — the one token every frame in this protocol carries and free-form
HTML/PHP-warning noise is not expected to — then walks backward from each
occurrence counting braces to find the nearest `{` not already closed by an
intervening `}`. That is brace-counting over raw text, not a string-aware
tokenizer: a noise line that happens to *contain* well-formed-looking JSON text
inside a quoted string (a PHP warning quoting a JSON fragment, an HTML attribute
holding one) can walk the count off by whatever braces sit inside that string.
The mitigation is not "get the brace right" — over raw text without a lexer that
cannot be guaranteed — it is that a wrong brace almost always produces text
`raw_decode` cannot parse as JSON at all, and an unparseable candidate is just
skipped, never guessed at. The one case this cannot catch is a forged frame: noise that contains a
*complete, valid* JSON object with a `"jsonrpc"` key and the exact `id`
being awaited. That id used to be the literal `2`, hardcoded and identical
on every call in all four adapters (#1935) — free to guess without
observing a single byte of the daemon's actual traffic. Each adapter now
sends `random.randrange(2**32)` per call and awaits that value instead, so
a forger with no visibility into the outgoing frame has on the order of a
1-in-4-billion chance of landing on the awaited id, rather than a
certainty. What remains open is narrower, and worth naming precisely
rather than waving at: anything that can *observe* the id — a process
sharing the daemon's stdout, a debugger attached to the call, a log line
that prints the outgoing frame — can still forge a match, because nothing
here authenticates the transport itself. What this module guarantees,
unchanged, is the cheaper, real property: it never returns an object it
could not fully decode, and it never returns one whose `id` does not
match.
"""
from __future__ import annotations

import json
import socket
import time

from linebreaks import split_lines


def _enclosing_brace(text: str, marker: int) -> int | None:
    """Index of the `{` that opens the object containing `text[marker]`, or
    `None` if none is found before the start of `text`.

    Walks backward counting `}` as it goes deeper and returning the first `{`
    seen at depth 0 — the innermost brace not already closed by a `}` between
    it and `marker`. Text-only brace counting, so a `{` or `}` inside a quoted
    string throws the count off; see the module docstring for what that costs.
    """
    depth = 0
    i = marker - 1
    while i >= 0:
        ch = text[i]
        if ch == "}":
            depth += 1
        elif ch == "{":
            if depth == 0:
                return i
            depth -= 1
        i -= 1
    return None


class DesyncDetected(RuntimeError):
    """`receive_until` gave up, but the buffer held a well-formed JSON-RPC
    *response* (a decodable object with an `id` key) that carried an id other
    than the one awaited (#2449).

    That is different evidence from silence or undecodable noise: it means a
    real frame was in the pipe and it was not ours -- the shape a daemon
    serving one client at a time leaves behind when a prior client
    disconnected mid-exchange and the next one inherited its tail end. A
    caller that sees this, rather than a plain `RuntimeError`, has a reason to
    believe a fresh connection to a freshly-spawned daemon might actually
    recover, which a caller retrying on pure silence (nothing ever arrived, or
    arrived but never parsed as JSON-RPC at all) does not have -- see
    `receive_until` for where the two are told apart.

    Subclasses `RuntimeError` so any existing `except RuntimeError` still
    catches it unchanged; only a caller that added `except DesyncDetected`
    sees the distinction at all.
    """


#: The `id` every one of the four warm adapters hardcodes for its own
#: `initialize` frame -- see each adapter's `ndjson_call`, `"id": 1`. A
#: response addressed to it is an expected part of *this* exchange, not
#: evidence of anything gone wrong, so `_scan` never treats it as a foreign
#: id (#2449): a slow tool call that has only had time to answer `initialize`
#: so far looks identical, on the wire, to nothing having happened yet.
INITIALIZE_ID = 1


def _scan(buf: bytes, want_id, own_ids: frozenset = frozenset((INITIALIZE_ID,))) -> tuple:
    """`(obj, saw_foreign_id)`: `obj` is the frame matching `want_id`, or
    `None`; `saw_foreign_id` is `True` iff some *other* well-formed JSON-RPC
    response was seen carrying an id that belongs to neither `want_id` nor
    `own_ids` (#2449).

    Scans for every `"jsonrpc"` occurrence rather than requiring one to start a
    line. For each, backs up to its enclosing `{` and attempts
    `json.JSONDecoder().raw_decode` from there — a candidate that fails to
    decode is skipped, never guessed at, and a candidate that decodes but
    carries the wrong `id` (an `initialize` response, a notification, a
    forged-looking fragment with an unrelated id) is passed over in favour of
    the next occurrence rather than rejected outright, since more than one
    JSON-RPC frame legitimately shares the buffer.

    `own_ids` exists because "some id other than the one I'm waiting for" is
    not, by itself, evidence of a desynchronised pipe: every one of these
    exchanges sends its own `initialize` frame first and gets its own
    response to it, and that response's id (`INITIALIZE_ID`) shows up in
    every buffer this scans, glued to nothing. Only an id belonging to
    *neither* this call nor its own preceding frames is a frame this
    exchange did not send for -- which is the actual signal a client that
    inherited another client's tail end would produce. A notification (no
    `id` key at all, e.g. `notifications/initialized`) is not evidence of
    anything either way.

    `buf` is decoded permissively (`errors="replace"`): a response glued to
    truncated multi-byte HTML is exactly the shape this exists for, and a
    `UnicodeDecodeError` here would trade the bug this fixes for a new one.
    """
    text = buf.decode("utf-8", errors="replace")
    decoder = json.JSONDecoder()
    search_from = 0
    saw_foreign_id = False
    while True:
        marker = text.find('"jsonrpc"', search_from)
        if marker == -1:
            return None, saw_foreign_id
        brace = _enclosing_brace(text, marker)
        if brace is None:
            search_from = marker + 1
            continue
        try:
            obj, _end = decoder.raw_decode(text, brace)
        except json.JSONDecodeError:
            search_from = marker + 1
            continue
        if isinstance(obj, dict):
            oid = obj.get("id")
            if oid == want_id:
                return obj, saw_foreign_id
            if "id" in obj and oid not in own_ids:
                saw_foreign_id = True
        search_from = marker + 1


def find_response(buf: bytes, want_id) -> dict | None:
    """The JSON-RPC object in `buf` whose `id` == `want_id`, or `None`.

    Thin wrapper over `_scan` that drops the desync signal, for the callers
    (and tests) that only ever wanted the object.
    """
    obj, _saw_foreign_id = _scan(buf, want_id)
    return obj


def describe_buffer(buf: bytes, want_id) -> str:
    """One line saying what was in `buf` when the scan gave up on `want_id`.

    Distinguishes *the daemon said nothing at all* from *the daemon said
    something and none of it was the awaited response* — the two states #1924
    asks to keep apart from a genuine "still working" timeout. An empty buffer
    is the first; a non-empty one that never yielded a matching frame is the
    second, and is reported with a byte count and how many `"jsonrpc"` markers
    were seen, so the message says what was actually there rather than
    reasserting the deadline.
    """
    if not buf:
        return "no bytes received"
    n_markers = buf.count(b'"jsonrpc"')
    plural = "s" if n_markers != 1 else ""
    return (f"received {len(buf)} bytes ({n_markers} \"jsonrpc\" marker{plural}), "
            f"none decoded to id={want_id!r}")


# #1927: a timeout used to say only that it waited, never what it received.
# The two facts below were sitting in reach the whole time -- the buffer
# already in hand, and the daemon's own log, written to disk as the exchange
# happens -- and neither reached the receipt.

DEFAULT_IDLE_TIMEOUT_S = 10.0


def read_daemon_log_tail(sock_path: str, n_lines: int = 8, max_chars: int = 400) -> str:
    """Last few non-blank lines of the daemon's own `<sock>.log`, or `''`.

    `daemon.py` opens `f"{sock_name}.log"` next to the socket for its whole
    life and writes to it as requests and responses happen (#1927's own
    incident: the answer left the server 2 seconds in, per that file, while
    the client waited out the remaining five minutes). Best effort only: a
    daemon predating this log, a runtime dir this process cannot read, or one
    that never got far enough to open the file are all silent `''` here
    rather than a second failure layered onto the timeout being reported.
    """
    try:
        with open(sock_path + ".log", "rb") as f:
            data = f.read()
    except OSError:
        return ""
    # #1486: split_lines(), not str.splitlines() -- this is a daemon's own log,
    # written by presets/mcp/daemon.py one line at a time, not a diagnostic
    # echoing arbitrary source text, but the log tail is folded into a single
    # RuntimeError message below and a stray U+2028/U+0085/VT/FF splitting it
    # into an extra "line" there would be the same self-inflicted count this
    # module exists to avoid one layer over.
    lines = [ln for ln in split_lines(data.decode("utf-8", errors="replace"))
             if ln.strip()]
    return " | ".join(lines[-n_lines:])[:max_chars]


def describe_timeout(buf: bytes, want_id, elapsed_s: float, sock_path: str) -> str:
    """One line for a call that gave up: what arrived, how long it took, and
    what the daemon's own log says -- the three facts #1927 names as the
    difference between a five-minute hunt and a single call. Elapsed time is
    stated explicitly (item 4): `NOT CHECKED` reads the same at 2s and at
    300s otherwise, and the receipt is the only place that cost is visible.
    """
    reason = describe_buffer(buf, want_id)
    if buf:
        # Silence and garbage are different failures (item 1): once bytes
        # exist, quote the start of them rather than only counting them, so
        # a stray HTML error page names itself instead of waiting to be
        # diagnosed by hand.
        preview = buf[:200].decode("utf-8", errors="replace")
        reason = f"{reason}; first bytes: {preview!r}"
    msg = f"no id={want_id!r} response after {elapsed_s:.1f}s ({reason})"
    log_tail = read_daemon_log_tail(sock_path)
    if log_tail:
        msg += f"; daemon log tail: {log_tail}"
    return msg


def receive_until(s: socket.socket, want_id, call_timeout: float, sock_path: str,
                   idle_timeout: float = DEFAULT_IDLE_TIMEOUT_S,
                   own_ids: frozenset = frozenset((INITIALIZE_ID,))) -> dict:
    """Read from `s` until a JSON-RPC frame with id == `want_id` arrives, or
    give up -- shared by all four warm adapters' `ndjson_call`.

    Two deadlines, not one (#1927 item 2). `call_timeout` bounds the whole
    exchange, same as before. `idle_timeout` is new and shorter: once at
    least one byte has arrived, no further bytes for `idle_timeout` means
    nothing more is coming -- a server that considers the exchange finished
    does not resume mid-`recv()` -- so this gives up then instead of waiting
    out whatever remains of `call_timeout`. Silence with zero bytes ever
    received is unaffected: that is still "genuinely still working" until
    `call_timeout`, because there is no last byte to measure idleness from.

    Raises on either deadline, naming what was received, how long was spent,
    and the daemon's own log tail (via `describe_timeout`). `DesyncDetected`
    -- a `RuntimeError` subclass -- if the buffer held a well-formed response
    to a *foreign* id along the way: one that is in neither `own_ids` nor
    `want_id` (#2449: evidence of a live but desynchronised pipe, worth a
    caller retrying against a fresh daemon -- the ordinary `initialize`
    response every exchange gets for itself does not count, see `_scan`).
    Plain `RuntimeError` otherwise (silence, or noise that never parsed as a
    JSON-RPC response at all).

    `own_ids` defaults to `{INITIALIZE_ID}`, the shared literal every
    adapter's own `initialize` frame used to carry -- **a caller that also
    randomises its own `initialize` id (as all four adapters now do, #2449)
    must pass its own value here instead of relying on the default**: a
    single hardcoded id shared by every client cannot tell "my own
    initialize reply" from "a foreign client's leftover initialize reply",
    since both would carry the identical id. See `_scan`'s own docstring.
    """
    start = time.monotonic()
    deadline = start + call_timeout
    buf = b""
    last_byte_at = None
    saw_other_id = False
    while True:
        now = time.monotonic()
        remaining = deadline - now
        if remaining <= 0:
            break
        if last_byte_at is not None:
            remaining = min(remaining, (last_byte_at + idle_timeout) - now)
            if remaining <= 0:
                break
        s.settimeout(remaining)
        try:
            chunk = s.recv(65536)
        except (socket.timeout, TimeoutError):
            break
        if not chunk:
            break
        buf += chunk
        last_byte_at = time.monotonic()
        obj, other = _scan(buf, want_id, own_ids)
        if other:
            saw_other_id = True
        if obj is not None:
            return obj
    msg = describe_timeout(buf, want_id, time.monotonic() - start, sock_path)
    if saw_other_id:
        # #2449: at least one well-formed response addressed to a different
        # id was seen -- a live but desynchronised pipe, not silence -- so
        # the caller gets a type it can choose to retry on, rather than an
        # indistinguishable plain timeout.
        raise DesyncDetected(msg)
    raise RuntimeError(msg)


def call_with_retry(do_call, respawn, pid_probe=None):
    """Run `do_call()` once more, against a freshly-respawned daemon, if it
    raises `DesyncDetected` -- and propagate everything else unchanged (#2449).

    This is the fix for the class the issue asks for, not a narrower one keyed
    to any particular root cause: whatever produced the desync (a client that
    disconnected mid-exchange and left its tail end for the next one to
    inherit is the reading the daemon's own contract supports, but nothing
    here depends on that being the only way to reach this state), a fresh
    daemon has no memory of it, so replaying the same exchange against one is
    the cheapest thing that can plausibly recover.

    Deliberately not "keep trying": at most one retry, and only on a signal
    that says a retry has a reason to help. Plain silence or noise that never
    parsed as a response at all raises `RuntimeError` from `receive_until`,
    not `DesyncDetected`, and is not retried by the first branch below --
    respawning and waiting out a second full `call_timeout` for a daemon (or
    an analysis) that is just genuinely slow or genuinely gone would turn
    every such call into a silent double-timeout for no corresponding chance
    of success. A second `DesyncDetected`, or any failure out of `respawn()`
    itself (`_spawn` raises its own exceptions -- `AutospawnSuppressed`, a
    plain `RuntimeError` on a spawn that never published a socket),
    propagates from the second `do_call()` exactly as the first would have:
    the caller's existing handling for "nothing could be checked" already
    covers it.

    **The second branch closes a gap this function's own first fix opened**
    (#2449, review round 2). The daemon behind these adapters is one shared
    process per `(cwd, name)`, serving every caller's exchange serially --
    so a *different*, perfectly healthy, currently in-flight exchange can be
    running against that same daemon at the exact moment another caller's
    `respawn()` (triggered by *its own*, unrelated `DesyncDetected`) reaps
    the process out from under it. That collateral caller sees a broken
    connection -- plain `RuntimeError`, not `DesyncDetected`, since nothing
    about a severed pipe looks like a foreign id -- and without this branch
    it would report `NOT CHECKED` unretried for a file with nothing wrong
    with it: a brand new instance of the exact "an innocent caller pays for
    someone else's disconnect" class #2449 was filed to close.

    `pid_probe`, when given, is a zero-argument callable returning the daemon's
    current pid (or a falsy value if none is running) -- read once before the
    first `do_call()` and again after a plain `RuntimeError`. A pid that
    changed between the two reads means the daemon `do_call()` was actually
    talking to no longer exists: something else replaced it mid-exchange, and
    the most likely something is exactly the collateral case above. That
    earns the same single retry a self-detected desync gets, without calling
    `respawn()` again -- a fresh, usable daemon already exists at the same
    socket path (whoever replaced it already published one), so there is
    nothing left to force. A pid that did *not* change is the ordinary case
    this function has always declined to retry: nothing changed underneath
    this caller, so the failure is whatever `receive_until` already
    determined it to be, and retrying it would be the double-timeout this
    function exists to avoid. Omitted entirely, this branch never fires and
    behaviour is identical to before it existed.

    `do_call` is a zero-argument callable that performs one whole exchange
    (connect, send, `receive_until`) and returns the parsed response;
    `respawn` is a zero-argument callable that forces a fresh daemon into
    existence and returns nothing this function reads -- both close over
    whatever adapter-specific state (the current socket path, in particular)
    they need, so this function stays a shared *retry*, not a shared
    *protocol*.
    """
    pid_before = pid_probe() if pid_probe is not None else None
    try:
        return do_call()
    except DesyncDetected:
        respawn()
        return do_call()
    except RuntimeError:
        if pid_probe is not None:
            pid_after = pid_probe()
            if pid_before and pid_after and pid_before != pid_after:
                return do_call()
        raise
