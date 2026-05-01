#!/usr/bin/env python3
"""Dev.to read: devto_read:ID_OR_SLUG_OR_URL

Aggregates: title, author, date, body, stats, tags, top N inline comments.
N via SUPERTOOL_INLINE_COMMENTS (default 5). Comments require numeric ID
(separate REST call); slug/URL flow only fetches article body.
"""
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))
from _auth import get_api_key
from _me import get_username
from _rest import request


def parse_arg(arg: str) -> tuple[str, dict[str, str]]:
    if not arg:
        sys.stderr.write("ERROR: usage devto_read:ID_OR_SLUG_OR_URL\n")
        sys.exit(2)
    if arg.startswith("http"):
        bits = urlparse(arg).path.strip("/").split("/")
        if len(bits) >= 2:
            return f"/articles/{bits[0]}/{bits[1]}", {}
    if arg.isdigit():
        return f"/articles/{arg}", {}
    return f"/articles/{arg}", {}


def own_engagement(comments: list[dict], me: str) -> tuple[int, list[str], str]:
    if not me:
        return 0, [], ""
    flat: list[dict] = []
    for c in comments:
        flat.append(c)
        flat.extend(c.get("children") or [])
    mine = [c for c in flat if (c.get("user") or {}).get("username") == me]
    if not mine:
        return 0, [], ""
    ids = [c.get("id_code") or str(c.get("id", "?")) for c in mine]
    last = max((c.get("created_at") or "") for c in mine).split("T")[0]
    return len(mine), ids, last


def render(a: dict, comments: list[dict], inline_n: int, me: str = "") -> str:
    date = (a.get("published_at") or "").split("T")[0]
    user = a.get("user") or {}
    body = a.get("body_markdown") or ""
    raw_tags = a.get("tag_list") or a.get("tags") or []
    if isinstance(raw_tags, str):
        raw_tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
    tags = ", ".join(raw_tags)
    head = (
        f"(article id={a.get('id')})\n"
        f"TITLE:    {a.get('title','?')}\n"
        f"AUTHOR:   {user.get('name','?')} (@{user.get('username','?')})\n"
        f"DATE:     {date}\n"
        f"URL:      {a.get('url','?')}\n"
        f"TAGS:     {tags or '(none)'}\n"
        f"STATS:    {a.get('public_reactions_count',0)} reactions, {a.get('comments_count',0)} comments"
    )
    n_mine, mine_ids, last = own_engagement(comments, me)
    if n_mine:
        head += f"\nYOU:      already commented {n_mine}× (ids: {', '.join(mine_ids)}) — last {last}"
    if comments:
        flat = []
        for c in comments[:inline_n]:
            user_c = (c.get("user") or {}).get("username", "?")
            cdate = (c.get("created_at") or "").split("T")[0]
            cid = c.get("id_code") or c.get("id") or "?"
            txt = (c.get("body_html") or "").replace("\n", " ").replace("<p>", "").replace("</p>", " ")[:200]
            flat.append(f"  [id={cid}] {cdate} @{user_c}: {txt}")
        cblock = "\n".join([f"--- top {len(flat)} comments ---"] + flat)
    else:
        cblock = "--- 0 comments ---"
    aid = a.get("id")
    nxt = (
        f"--- NEXT ---\n"
        f"  devto_react:{aid}              — like this article\n"
        f"  devto_react:{aid}|unicorn      — unicorn reaction\n"
        f"  devto_comments:{aid}:N         — read more comments\n"
        f"  (no comment write API on Dev.to — comments via web only)"
    )
    return f"{head}\n{cblock}\n--- body ---\n{body}\n{nxt}"


def main(arg: str) -> None:
    path, query = parse_arg(arg)
    api_key = get_api_key()
    article = request("GET", path, api_key, query=query)
    if not article or not isinstance(article, dict):
        sys.stderr.write(f"ERROR: article not found: {arg}\n")
        sys.exit(1)
    aid = article.get("id")
    comments: list[dict] = []
    if aid:
        c = request("GET", "/comments", api_key, query={"a_id": aid})
        if isinstance(c, list):
            comments = c
    inline_n = int(os.environ.get("SUPERTOOL_INLINE_COMMENTS", "5"))
    me = get_username(api_key)
    print(render(article, comments, inline_n, me))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
