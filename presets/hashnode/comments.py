#!/usr/bin/env python3
"""Hashnode comments: hashnode_comments:SLUG_OR_URL[:N]"""
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))
from _auth import get_publication_id, get_token
from _graphql import gql
from _outbound import my_comment_ids, read as read_outbound
from _sanitize import safe_short

QUERY = """
query Comments($publicationId: ObjectId!, $slug: String!, $first: Int!) {
  publication(id: $publicationId) {
    post(slug: $slug) {
      id title
      comments(first: $first) {
        edges { node {
          id dateAdded content { markdown }
          author { username name }
        } }
      }
    }
  }
}
"""


def parse_args(arg: str) -> tuple[str, int]:
    if not arg:
        sys.stderr.write("ERROR: usage hashnode_comments:SLUG_OR_URL[:N]\n")
        sys.exit(2)
    default_n = int(os.environ.get("SUPERTOOL_DEFAULT_LIMIT", "20"))
    if arg.startswith("http"):
        path = urlparse(arg).path.strip("/")
        slug = path.split("/")[-1]
        return slug, default_n
    parts = arg.split(":")
    slug = parts[0]
    n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else default_n
    return slug, n


def render(slug: str, post: dict | None, mine: set[str] | None = None) -> str:
    mine = mine or set()
    if not post:
        return f"(post not found: {slug})"
    edges = (post.get("comments") or {}).get("edges", [])
    if not edges:
        return f"(0 comments on {post.get('title', slug)})"
    out = [f"({len(edges)} comments on {post.get('title', slug)})"]
    for e in edges:
        c = e["node"]
        author = c.get("author") or {}
        date = (c.get("dateAdded") or "").split("T")[0]
        raw_body = (c.get("content") or {}).get("markdown") or ""
        body = safe_short(raw_body, 300)
        cid = c.get("id") or ""
        you = "[YOU] " if cid and cid in mine else ""
        username = safe_short(author.get("username") or "?", 60)
        out.append(f"- {you}[id={cid}] {date} @{username}: {body}")
    return "\n".join(out)


def main(arg: str) -> None:
    slug, n = parse_args(arg)
    token = get_token()
    pub_id = get_publication_id()
    data = gql(QUERY, {"publicationId": pub_id, "slug": slug, "first": n}, token)
    post = (data.get("publication") or {}).get("post")
    mine = my_comment_ids(read_outbound())
    print(render(slug, post, mine))


if __name__ == "__main__":
    # Supertool {args} passes parts space-separated; rejoin with ':' to reconstruct
    # the canonical 'slug[:N]' shape parse_args expects.
    arg = ":".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    main(arg)
