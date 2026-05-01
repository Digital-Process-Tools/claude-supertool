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
from _me import get_username

POST_FIELDS = """
  id title url publishedAt reactionCount responseCount
  author { username name }
  tags { slug name }
  content { markdown }
  comments(first: $cFirst) {
    edges { node { id dateAdded content { markdown } author { username } } }
  }
"""

SLUG_BY_PUB_ID = """
query SlugByPubId($publicationId: ObjectId!, $slug: String!, $cFirst: Int!) {
  publication(id: $publicationId) { post(slug: $slug) {
    %s
  } }
}
""" % POST_FIELDS

SLUG_BY_HOST = """
query SlugByHost($host: String!, $slug: String!, $cFirst: Int!) {
  publication(host: $host) { post(slug: $slug) {
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


def parse_arg(arg: str) -> tuple[str | None, str | None, str | None]:
    """Returns (slug, post_id, host). Exactly one of slug/post_id is set;
    host is set only when arg was a URL pointing to a non-default publication."""
    if not arg:
        sys.stderr.write("ERROR: usage hashnode_read:SLUG_OR_URL\n")
        sys.exit(2)
    if arg.startswith("http"):
        parsed = urlparse(arg)
        return parsed.path.strip("/").split("/")[-1], None, parsed.netloc
    if all(c in "0123456789abcdef" for c in arg.lower()) and len(arg) >= 12:
        return None, arg, None
    return arg, None, None


def own_engagement(post: dict, me: str) -> tuple[int, list[str], str]:
    """Return (count, comment_ids, last_date) for comments authored by `me`."""
    if not me:
        return 0, [], ""
    edges = (post.get("comments") or {}).get("edges", [])
    mine = [e["node"] for e in edges
            if (e["node"].get("author") or {}).get("username") == me]
    if not mine:
        return 0, [], ""
    ids = [c.get("id", "?") for c in mine]
    last = max((c.get("dateAdded") or "") for c in mine).split("T")[0]
    return len(mine), ids, last


def render(post: dict, inline_n: int, me: str = "") -> str:
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
    n_mine, mine_ids, last = own_engagement(post, me)
    if n_mine:
        head += f"\nYOU:      already commented {n_mine}× (ids: {', '.join(mine_ids)}) — last {last}"
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
    slug, post_id, host = parse_arg(arg)
    inline_n = int(os.environ.get("SUPERTOOL_INLINE_COMMENTS", "5"))
    scan_n = max(inline_n, int(os.environ.get("SUPERTOOL_SCAN_COMMENTS", "50")))
    if post_id is not None:
        data = gql(ID_QUERY, {"id": post_id, "cFirst": scan_n}, token)
        post = data.get("post")
    elif host:
        data = gql(SLUG_BY_HOST, {"host": host, "slug": slug, "cFirst": scan_n}, token)
        post = (data.get("publication") or {}).get("post")
    else:
        pub_id = get_publication_id()
        data = gql(SLUG_BY_PUB_ID, {"publicationId": pub_id, "slug": slug, "cFirst": scan_n}, token)
        post = (data.get("publication") or {}).get("post")
    if not post:
        sys.stderr.write(f"ERROR: post not found: {arg}\n")
        sys.exit(1)
    me = get_username(token)
    print(render(post, inline_n, me))


if __name__ == "__main__":
    arg = ":".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    main(arg)
