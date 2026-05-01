#!/usr/bin/env python3
"""Hashnode search: hashnode_search:QUERY[:N] — text search on configured publication."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _auth import get_publication_id, get_token
from _graphql import gql
from _sanitize import safe_short

QUERY = """
query Search($publicationId: ObjectId!, $query: String!, $first: Int!) {
  searchPostsOfPublication(
    first: $first
    filter: {query: $query, publicationId: $publicationId}
  ) {
    edges { node {
      id title slug url publishedAt reactionCount responseCount
      author { username }
    } }
  }
}
"""


def parse_args(arg: str) -> tuple[str, int]:
    if not arg:
        sys.stderr.write("ERROR: usage hashnode_search:QUERY[:N]\n")
        sys.exit(2)
    parts = arg.rsplit(":", 1)
    if len(parts) > 1 and parts[1].isdigit():
        return parts[0], int(parts[1])
    default_n = int(os.environ.get("SUPERTOOL_DEFAULT_LIMIT", "10"))
    return arg, default_n


def render(query: str, posts: list[dict]) -> str:
    if not posts:
        return f"(no results for {query!r})"
    out = [f"({len(posts)} results for {query!r})"]
    for p in posts:
        au = safe_short((p.get("author") or {}).get("username", "?"), 60)
        date = (p.get("publishedAt") or "").split("T")[0]
        title = safe_short(p.get("title") or "?", 120)
        out.append(
            f"- {date} {title!r} by @{au} → {p['url']} "
            f"({p.get('reactionCount',0)} reactions, {p.get('responseCount',0)} comments)"
        )
    return "\n".join(out)


def main(arg: str) -> None:
    query, n = parse_args(arg)
    token = get_token()
    pub_id = get_publication_id()
    data = gql(QUERY, {"publicationId": pub_id, "query": query, "first": n}, token)
    edges = (data.get("searchPostsOfPublication") or {}).get("edges", [])
    print(render(query, [e["node"] for e in edges]))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
