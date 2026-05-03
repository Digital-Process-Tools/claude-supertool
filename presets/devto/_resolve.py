"""Dev.to article ID resolver.

The Reactions and Comments write endpoints require the integer article ID;
sending a slug or URL returns 422 "Reactable not valid". This helper resolves
any of the four input shapes into the numeric ID by calling the appropriate
Dev.to API endpoint when needed.

Accepted inputs:
  - "12345"                                        → 12345
  - "author/article-slug"                          → resolved via /articles/author/slug
  - "https://dev.to/author/article-slug"           → URL parsed → resolved
  - "https://dev.to/author/article-slug-1234abcd"  → suffixed slug → resolved
  - "article-slug-with-suffix-3j3e"               → bare slug (no author, no scheme)
                                                      → resolved via /articles?slug=...
"""
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))
from _auth import get_api_key
from _rest import request

_BARE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*-[a-z0-9]{4,6}$")


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
    path, query = _to_api_path_and_query(s)
    if path is None:
        sys.stderr.write(
            f"ERROR: cannot parse article identifier {raw!r} — "
            "expected numeric ID, 'author/slug', bare slug (e.g. my-post-3j3e), "
            "or full https://dev.to/... URL\n"
        )
        sys.exit(2)
    result = request("GET", path, get_api_key(), query=query)
    if isinstance(result, list):
        if not result or not isinstance(result[0], dict) or not result[0].get("id"):
            sys.stderr.write(
                f"ERROR: could not resolve {raw!r} to article ID — "
                "Dev.to returned no results. Check the slug is correct.\n"
            )
            sys.exit(1)
        return int(result[0]["id"])
    if not isinstance(result, dict) or not result.get("id"):
        sys.stderr.write(
            f"ERROR: could not resolve {raw!r} to article ID — "
            "Dev.to returned no id field. Check the slug/URL is correct.\n"
        )
        sys.exit(1)
    return int(result["id"])


def _to_api_path_and_query(s: str) -> tuple[str | None, dict | None]:
    """Convert a slug or URL into an /articles/... API path plus optional query params.

    Returns (path, query) where query is None for author/slug and URL forms,
    and {"per_page": 1, "slug": s} for bare-slug form.
    """
    if s.startswith("http"):
        bits = urlparse(s).path.strip("/").split("/")
        if len(bits) >= 2:
            return f"/articles/{bits[0]}/{bits[1]}", None
        return None, None
    if "/" in s:
        return f"/articles/{s}", None
    if _BARE_SLUG_RE.match(s):
        return "/articles", {"per_page": 1, "slug": s}
    return None, None
