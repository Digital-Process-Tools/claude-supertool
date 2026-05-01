#!/usr/bin/env python3
"""Hashnode browse: hashnode_browse:TAG[:N][:SORT]

SORT: recent (default) or top.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _auth import get_token
from _graphql import gql

QUERY_TEMPLATE = """
query TagFeed($slug: String!, $first: Int!) {
  tag(slug: $slug) {
    posts(first: $first, filter: {sortBy: %s}) {
      edges { node {
        id title url publishedAt reactionCount responseCount
        author { username name }
      } }
    }
  }
}
"""

SORT_MAP = {"recent": "recent", "top": "popular", "popular": "popular"}


def parse_args(arg: str) -> tuple[str, int, str]:
    if not arg:
        sys.stderr.write("ERROR: usage hashnode_browse:TAG[:N][:SORT]\n")
        sys.exit(2)
    parts = arg.split(":")
    tag = parts[0]
    default_n = int(os.environ.get("SUPERTOOL_DEFAULT_LIMIT", "10"))
    n = default_n
    sort = "recent"
    for p in parts[1:]:
        if p.isdigit():
            n = int(p)
        elif p in SORT_MAP:
            sort = SORT_MAP[p]
        else:
            sys.stderr.write(f"ERROR: unknown sort/limit token {p!r}; use a number or one of {sorted(SORT_MAP)}\n")
            sys.exit(2)
    return tag, n, sort


def render(tag: str, posts: list[dict], sort: str = "recent") -> str:
    if not posts:
        return f"(no posts on tag {tag})"
    out = [f"({len(posts)} posts on tag {tag}, sort={sort})"]
    for p in posts:
        author = p.get("author") or {}
        date = (p.get("publishedAt") or "").split("T")[0]
        out.append(
            f"- {date} {p['title']!r} by @{author.get('username','?')} → {p['url']} "
            f"({p.get('reactionCount',0)} reactions, {p.get('responseCount',0)} comments)"
        )
    return "\n".join(out)


def main(arg: str) -> None:
    tag, n, sort = parse_args(arg)
    token = get_token()
    query = QUERY_TEMPLATE % sort
    data = gql(query, {"slug": tag, "first": n}, token)
    edges = ((data.get("tag") or {}).get("posts") or {}).get("edges", [])
    print(render(tag, [e["node"] for e in edges], sort))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
