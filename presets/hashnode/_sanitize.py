"""Prompt-injection mitigation for external content.

External text (post bodies, comments, search results) flows into the
LLM context every time we read it. A malicious user can embed
instructions ("ignore previous instructions...") that try to hijack
the next op call.

Defense layers:

1. Wrapping — every chunk of external text is wrapped in
   `<<UNTRUSTED CONTENT — START <nonce>>> ... <<END ... <nonce>>>`
   markers. The LLM is trained to treat tagged regions as data, not
   instructions. The nonce is per call and the wrapped text cannot
   predict it, so content cannot close the region from inside; any
   fence-shaped run in the content is neutralised on the way in (#693).

2. Heuristic flag — known injection patterns get prefixed with a
   ⚠ POSSIBLE INJECTION warning so the human reviewer notices before
   firing the next op. A *clean* scan says so as well: silence would be
   the same output as content that was never scanned at all, and the two
   are not the same claim.

3. (Action gate, separate) — engagement queue + Florian-fires-only.

This file is duplicated per preset dir (bluesky/devto/hashnode) so
each preset stays self-contained. Keep them in sync.
"""
from __future__ import annotations

import re
import secrets

# What a clean scan says. Printing nothing made "the scanner found nothing" and
# "there was nothing scanning" one output — the house defect (#693), on the
# layer whose whole job is to be suspicious. It states the limit of the claim
# too: this is a pattern list, not a proof.
SCAN_CLEAN_NOTE = (
    "[scan] no known injection patterns matched (heuristic — not a guarantee)"
)

# Anything fence-shaped in the content itself. Neutralised rather than removed,
# so the reader can see something was there.
_FENCE_LIKE = re.compile(r"<<\s*(?:END\s+)?UNTRUSTED[^>]*>>", re.IGNORECASE)
_NEUTRALISED = "[fence marker in content — neutralised]"

# Known injection trigger phrases — case-insensitive.
_PATTERNS = [
    re.compile(r"ignore\s+(?:all\s+|previous\s+|earlier\s+|the\s+above\s+)?(?:prior\s+)?(?:instructions|prompts|context)", re.IGNORECASE),
    re.compile(r"disregard\s+(?:all\s+|previous\s+|earlier\s+|the\s+above\s+)", re.IGNORECASE),
    re.compile(r"you\s+are\s+(?:now\s+|a\s+|an\s+)", re.IGNORECASE),
    re.compile(r"(?:^|\n)\s*system\s*:", re.IGNORECASE),
    re.compile(r"</?(?:system|assistant|human|user)>", re.IGNORECASE),
    re.compile(r"reveal\s+your\s+(?:system\s+prompt|instructions|prompt)", re.IGNORECASE),
    re.compile(r"print\s+your\s+(?:system\s+prompt|instructions)", re.IGNORECASE),
    re.compile(r"new\s+(?:instructions|task|directive)\s*:", re.IGNORECASE),
    re.compile(r"forget\s+(?:everything|all|prior)", re.IGNORECASE),
    re.compile(r"override\s+(?:all\s+)?(?:previous\s+)?(?:instructions|rules)", re.IGNORECASE),
]

# Long base64-looking blobs (often used to hide instructions)
_BASE64_BLOB = re.compile(r"[A-Za-z0-9+/]{80,}={0,2}")


def detect(text: str) -> list[str]:
    """Return a list of detected injection-pattern names. Empty = clean.

    Empty is an answer, not an absence of one — but only the caller can render
    it, since this returns a list and prints nothing. `wrap` states it; see
    SCAN_CLEAN_NOTE. `safe_short` deliberately does not: it renders once per
    row of every list and browse, and a disclaimer on every row is one nobody
    reads on the row that needed it.
    """
    if not text:
        return []
    hits: list[str] = []
    for p in _PATTERNS:
        m = p.search(text)
        if m:
            hits.append(m.group(0)[:60])
    if _BASE64_BLOB.search(text):
        hits.append("long base64-looking blob")
    return hits


def wrap(text: str, source: str = "external") -> str:
    """Wrap external text in untrusted-content markers, prefix warning if injection patterns matched."""
    if not text:
        return text
    hits = detect(text)
    # A fixed delimiter is one the attacker can write down. The old markers were
    # constants, so a post body containing `<<END UNTRUSTED CONTENT>>` closed the
    # region early and everything after it read as trusted — and the test suite
    # asserted only that the markers were *present*, which stayed true while it
    # happened (#693). Two layers, because either alone is thin: a per-call nonce
    # the content cannot guess, and a scrub of fence-shaped runs so a human
    # skimming the block is not fooled either.
    nonce = secrets.token_hex(4)
    body = _FENCE_LIKE.sub(_NEUTRALISED, text)
    header = f"<<UNTRUSTED {source.upper()} CONTENT — START {nonce}>>"
    footer = f"<<END UNTRUSTED CONTENT {nonce}>>"
    if hits:
        lead = f"⚠ POSSIBLE INJECTION — review carefully ({', '.join(hits[:3])})\n"
    else:
        lead = f"{SCAN_CLEAN_NOTE}\n"
    return f"{lead}{header}\n{body}\n{footer}"


def safe_short(text: str, max_len: int = 200) -> str:
    """For inline previews — strip newlines, truncate, prefix ⚠ when injection detected.

    Used in list/browse/search/comments renders for titles, bodies, usernames.
    Adds an inline warning marker (no full <<UNTRUSTED ... >> block — too noisy
    for one-line items). Use wrap() for full bodies in read ops.
    """
    if not text:
        return ""
    flat = text.replace("\n", " ")[:max_len]
    if detect(flat):
        return f"⚠ {flat}"
    return flat
