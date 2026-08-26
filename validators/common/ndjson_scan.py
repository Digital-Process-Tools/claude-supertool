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


def find_response(buf: bytes, want_id) -> dict | None:
    """The JSON-RPC object in `buf` whose `id` == `want_id`, or `None`.

    Scans for every `"jsonrpc"` occurrence rather than requiring one to start a
    line. For each, backs up to its enclosing `{` and attempts
    `json.JSONDecoder().raw_decode` from there — a candidate that fails to
    decode is skipped, never guessed at, and a candidate that decodes but
    carries the wrong `id` (an `initialize` response, a notification, a
    forged-looking fragment with an unrelated id) is passed over in favour of
    the next occurrence rather than rejected outright, since more than one
    JSON-RPC frame legitimately shares the buffer.

    `buf` is decoded permissively (`errors="replace"`): a response glued to
    truncated multi-byte HTML is exactly the shape this exists for, and a
    `UnicodeDecodeError` here would trade the bug this fixes for a new one.
    """
    text = buf.decode("utf-8", errors="replace")
    decoder = json.JSONDecoder()
    search_from = 0
    while True:
        marker = text.find('"jsonrpc"', search_from)
        if marker == -1:
            return None
        brace = _enclosing_brace(text, marker)
        if brace is None:
            search_from = marker + 1
            continue
        try:
            obj, _end = decoder.raw_decode(text, brace)
        except json.JSONDecodeError:
            search_from = marker + 1
            continue
        if isinstance(obj, dict) and obj.get("id") == want_id:
            return obj
        search_from = marker + 1


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
                   idle_timeout: float = DEFAULT_IDLE_TIMEOUT_S) -> dict:
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

    Raises `RuntimeError(describe_timeout(...))` on either deadline, naming
    what was received, how long was spent, and the daemon's own log tail.
    """
    start = time.monotonic()
    deadline = start + call_timeout
    buf = b""
    last_byte_at = None
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
        obj = find_response(buf, want_id)
        if obj is not None:
            return obj
    raise RuntimeError(describe_timeout(buf, want_id, time.monotonic() - start, sock_path))
