#!/usr/bin/env python3
"""Hashnode read: hashnode_read:SLUG_OR_URL

Aggregates: title, author, date, body, stats, top N inline comments, tags.
N comments via SUPERTOOL_INLINE_COMMENTS (default 5).
"""
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))
from _auth import get_publication_id, get_token
from _graphql import gql

POST_FIELDS = """
  id title url publishedAt reactionCount responseCount
  author { username name }
  tags { slug name }
  content { markdown }
  comments(first: $cFirst) {
    edges { node { id dateAdded content { markdown } author { username } } }
  }
"""

SLUG_QUERY = """
query SlugRead($publicationId: ObjectId!, $slug: String!, $cFirst: Int!) {
  publication(id: $publicationId) { post(slug: $slug) {
    %s
  } }
}
""" % POST_FIELDS

ID_QUERY = """
query IdRead($id: ID!, $cFirst: Int!) {
  post(id: $id) {
    %s
  }
}
""" % POST_FIELDS


def parse_arg(arg: str) -> tuple[str | None, str | None]:
    if not arg:
        sys.stderr.write("ERROR: usage hashnode_read:SLUG_OR_URL\n")
        sys.exit(2)
    if arg.startswith("http"):
        return urlparse(arg).path.strip("/").split("/")[-1], None
    if all(c in "0123456789abcdef" for c in arg.lower()) and len(arg) >= 12:
        return None, arg
    return arg, None


def render(post: dict, inline_n: int) -> str:
    author = post.get("author") or {}
    body = (post.get("content") or {}).get("markdown") or ""
    date = (post.get("publishedAt") or "").split("T")[0]
    tags = ", ".join(t["slug"] for t in (post.get("tags") or []))
    head = (
        f"(post id={post['id']})\n"
        f"TITLE:    {post['title']}\n"
        f"AUTHOR:   {author.get('name','?')} (@{author.get('username','?')})\n"
        f"DATE:     {date}\n"
        f"URL:      {post['url']}\n"
        f"TAGS:     {tags or '(none)'}\n"
        f"STATS:    {post.get('reactionCount',0)} reactions, {post.get('responseCount',0)} comments"
    )
    edges = (post.get("comments") or {}).get("edges", [])[:inline_n]
    if edges:
        cblock = [f"--- top {len(edges)} comments ---"]
        for e in edges:
            c = e["node"]
            au = (c.get("author") or {}).get("username", "?")
            cdate = (c.get("dateAdded") or "").split("T")[0]
            cid = c.get("id", "?")
            txt = ((c.get("content") or {}).get("markdown") or "").replace("\n", " ")[:200]
            cblock.append(f"  [id={cid}] {cdate} @{au}: {txt}")
        comments_section = "\n".join(cblock)
    else:
        comments_section = "--- 0 comments ---"
    pid = post["id"]
    nxt = (
        f"--- NEXT ---\n"
        f"  hashnode_react:{pid}           — like this post\n"
        f"  hashnode_comment:{pid}|MSG     — comment on this post\n"
        f"  hashnode_comments:{post.get('url','')}:N — read more comments"
    )
    return f"{head}\n{comments_section}\n--- body ---\n{body}\n{nxt}"


def main(arg: str) -> None:
    token = get_token()
    slug, post_id = parse_arg(arg)
    inline_n = int(os.environ.get("SUPERTOOL_INLINE_COMMENTS", "5"))
    if slug is not None:
        pub_id = get_publication_id()
        data = gql(SLUG_QUERY, {"publicationId": pub_id, "slug": slug, "cFirst": inline_n}, token)
        post = (data.get("publication") or {}).get("post")
    else:
        data = gql(ID_QUERY, {"id": post_id, "cFirst": inline_n}, token)
        post = data.get("post")
    if not post:
        sys.stderr.write(f"ERROR: post not found: {arg}\n")
        sys.exit(1)
    print(render(post, inline_n))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
