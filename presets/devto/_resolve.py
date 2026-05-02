"""Dev.to article ID resolver.

The Reactions and Comments write endpoints require the integer article ID;
sending a slug or URL returns 422 "Reactable not valid". This helper resolves
any of the three input shapes into the numeric ID by calling /articles/{slug}
when needed.

Accepted inputs:
  - "12345"                                        → 12345
  - "author/article-slug"                          → resolved via API
  - "https://dev.to/author/article-slug"           → URL parsed → resolved
  - "https://dev.to/author/article-slug-1234abcd"  → suffixed slug → resolved
"""
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))
from _auth import get_api_key
from _rest import request


def resolve_article_id(raw: str) -> int:
    """Return the numeric article ID for any accepted input shape.

    Exits the process with a clear error if the input cannot be resolved.
    """
    s = (raw or "").strip()
    if not s:
        sys.stderr.write("ERROR: empty article identifier\n")
        sys.exit(2)
    if s.isdigit():
        return int(s)
    path = _to_api_path(s)
    if path is None:
        sys.stderr.write(
            f"ERROR: cannot parse article identifier {raw!r} — "
            "expected numeric ID, 'author/slug', or full https://dev.to/... URL\n"
        )
        sys.exit(2)
    article = request("GET", path, get_api_key())
    if not isinstance(article, dict) or not article.get("id"):
        sys.stderr.write(
            f"ERROR: could not resolve {raw!r} to article ID — "
            "Dev.to returned no id field. Check the slug/URL is correct.\n"
        )
        sys.exit(1)
    return int(article["id"])


def _to_api_path(s: str) -> str | None:
    """Convert a slug or URL into an /articles/... API path."""
    if s.startswith("http"):
        bits = urlparse(s).path.strip("/").split("/")
        if len(bits) >= 2:
            return f"/articles/{bits[0]}/{bits[1]}"
        return None
    if "/" in s:
        return f"/articles/{s}"
    return None
