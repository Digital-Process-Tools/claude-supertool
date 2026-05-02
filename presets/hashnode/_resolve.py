"""Hashnode post ID resolver.

The original `resolve_post_id` in react.py / comment.py only worked for posts
in the caller's own publication (`get_publication_id()`). Foreign URLs like
`https://hugovalters.hashnode.dev/some-slug` returned "post not found" because
the slug was looked up under the wrong publication.

This helper parses the host from the URL and uses the GraphQL
`publication(host:)` field, which works across all Hashnode publications.

Accepted inputs:
  - "objectid-hex-string"                                → returned as-is
  - "https://author.hashnode.dev/slug"                   → cross-publication lookup
  - "https://blog.example.com/slug" (custom domain)      → cross-publication lookup
"""
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))
from _graphql import gql


RESOLVE_BY_HOST = """
query ResolveByHost($host: String!, $slug: String!) {
  publication(host: $host) { post(slug: $slug) { id } }
}
"""


def resolve_post_id(token: str, arg: str) -> str:
    """Return the Hashnode post ID for an ID, own-publication URL, or foreign URL.

    Exits the process with a clear error if the input cannot be resolved.
    """
    s = (arg or "").strip()
    if not s:
        sys.stderr.write("ERROR: empty post identifier\n")
        sys.exit(2)
    if not s.startswith("http"):
        return s
    parsed = urlparse(s)
    host = parsed.netloc
    slug = parsed.path.strip("/").split("/")[-1]
    if not host or not slug:
        sys.stderr.write(f"ERROR: cannot parse Hashnode URL {arg!r}\n")
        sys.exit(2)
    data = gql(RESOLVE_BY_HOST, {"host": host, "slug": slug}, token)
    pub = data.get("publication") or {}
    post = pub.get("post")
    if not post:
        sys.stderr.write(
            f"ERROR: post not found at {arg!r} — host={host!r} slug={slug!r}. "
            "Check the URL is a valid Hashnode post.\n"
        )
        sys.exit(1)
    return post["id"]
