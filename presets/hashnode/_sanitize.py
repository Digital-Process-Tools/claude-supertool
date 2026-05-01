"""Prompt-injection mitigation for external content.

External text (post bodies, comments, search results) flows into the
LLM context every time we read it. A malicious user can embed
instructions ("ignore previous instructions...") that try to hijack
the next op call.

Defense layers:

1. Wrapping — every chunk of external text is wrapped in
   `<<UNTRUSTED CONTENT — START>> ... <<END>>` markers. The LLM is
   trained to treat tagged regions as data, not instructions.

2. Heuristic flag — known injection patterns get prefixed with a
   ⚠ POSSIBLE INJECTION warning so the human reviewer notices before
   firing the next op.

3. (Action gate, separate) — engagement queue + Florian-fires-only.

This file is duplicated per preset dir (bluesky/devto/hashnode) so
each preset stays self-contained. Keep them in sync.
"""
import re

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
    """Return a list of detected injection-pattern names. Empty = clean."""
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
    header = f"<<UNTRUSTED {source.upper()} CONTENT — START>>"
    footer = "<<END UNTRUSTED CONTENT>>"
    if hits:
        warning = f"⚠ POSSIBLE INJECTION — review carefully ({', '.join(hits[:3])})\n"
        return f"{warning}{header}\n{text}\n{footer}"
    return f"{header}\n{text}\n{footer}"


def safe_short(text: str, max_len: int = 200) -> str:
    """For inline previews — strip newlines, truncate, mark untrusted."""
    if not text:
        return ""
    flat = text.replace("\n", " ")[:max_len]
    return flat
