#!/usr/bin/env python3
"""Hashnode browse: hashnode_browse:TAG[:N]

Recent posts on a tag (cross-publication feed).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _auth import get_token
from _graphql import gql

QUERY = """
query TagFeed($slug: String!, $first: Int!) {
  tag(slug: $slug) {
    posts(first: $first, filter: {sortBy: recent}) {
      edges { node {
        id title url publishedAt reactionCount responseCount
        author { username name }
      } }
    }
  }
}
"""


def parse_args(arg: str) -> tuple[str, int]:
    if not arg:
        sys.stderr.write("ERROR: usage hashnode_browse:TAG[:N]\n")
        sys.exit(2)
    parts = arg.split(":")
    tag = parts[0]
    default_n = int(os.environ.get("SUPERTOOL_DEFAULT_LIMIT", "10"))
    n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else default_n
    return tag, n


def render(tag: str, posts: list[dict]) -> str:
    if not posts:
        return f"(no posts on tag {tag})"
    out = [f"({len(posts)} posts on tag {tag})"]
    for p in posts:
        author = p.get("author") or {}
        date = (p.get("publishedAt") or "").split("T")[0]
        out.append(
            f"- {date} {p['title']!r} by @{author.get('username','?')} → {p['url']} "
            f"({p.get('reactionCount',0)} reactions, {p.get('responseCount',0)} comments)"
        )
    return "\n".join(out)


def main(arg: str) -> None:
    tag, n = parse_args(arg)
    token = get_token()
    data = gql(QUERY, {"slug": tag, "first": n}, token)
    edges = ((data.get("tag") or {}).get("posts") or {}).get("edges", [])
    print(render(tag, [e["node"] for e in edges]))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
