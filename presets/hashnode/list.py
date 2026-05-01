#!/usr/bin/env python3
"""Hashnode list: hashnode_list[:N] (own publication) | hashnode_list:USER[:N]

N defaults to SUPERTOOL_DEFAULT_LIMIT or 10.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _auth import get_publication_id, get_token
from _graphql import gql
from _sanitize import safe_short

PUB_QUERY = """
query PubPosts($id: ObjectId!, $first: Int!) {
  publication(id: $id) {
    posts(first: $first) {
      edges { node { id title slug url publishedAt reactionCount responseCount } }
    }
  }
}
"""

USER_QUERY = """
query UserPosts($username: String!, $first: Int!) {
  user(username: $username) {
    publications(first: 1) {
      edges { node { posts(first: $first) {
        edges { node { id title slug url publishedAt reactionCount responseCount } }
      } } }
    }
  }
}
"""


def parse_args(arg: str) -> tuple[str | None, int]:
    """Returns (username_or_None, limit)."""
    default_n = int(os.environ.get("SUPERTOOL_DEFAULT_LIMIT", "10"))
    if not arg or arg.isdigit():
        return None, int(arg) if arg else default_n
    parts = arg.split(":")
    user = parts[0]
    n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else default_n
    return user, n


def render_posts(posts: list[dict]) -> str:
    if not posts:
        return "(no posts)"
    out = [f"({len(posts)} posts)"]
    for p in posts:
        date = (p.get("publishedAt") or "").split("T")[0]
        title = safe_short(p.get("title") or "?", 120)
        out.append(
            f"- {date} {title!r} → {p['url']} "
            f"({p.get('reactionCount', 0)} reactions, {p.get('responseCount', 0)} comments)"
        )
    return "\n".join(out)


def main(arg: str) -> None:
    token = get_token()
    user, n = parse_args(arg)
    if user is None:
        pub_id = get_publication_id()
        data = gql(PUB_QUERY, {"id": pub_id, "first": n}, token)
        edges = (data.get("publication") or {}).get("posts", {}).get("edges", [])
    else:
        data = gql(USER_QUERY, {"username": user, "first": n}, token)
        u = data.get("user") or {}
        pub_edges = u.get("publications", {}).get("edges", [])
        edges = pub_edges[0]["node"]["posts"]["edges"] if pub_edges else []
    print(render_posts([e["node"] for e in edges]))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
