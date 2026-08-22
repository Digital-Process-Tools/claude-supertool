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
skipped, never guessed at. The one case this cannot catch is a forged frame:
noise that contains a *complete, valid* JSON object with a `"jsonrpc"` key and
the exact `id` being awaited. Nothing short of an authenticated transport closes
that gap, and this module does not claim to. What it guarantees is the cheaper,
real property: it never returns an object it could not fully decode, and it never
returns one whose `id` does not match.
"""
from __future__ import annotations

import json


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
