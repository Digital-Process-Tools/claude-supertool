#!/usr/bin/env python3
"""Hashnode status_since: hashnode_status_since[:ISO_TIMESTAMP]

Aggregated activity briefing across own publication: new comments,
follower count delta, top-engaged posts since the given timestamp.
No arg = uses ~/.config/hashnode/last_check (auto-tracking).

State file is updated with current time on success — so successive
calls naturally show "what's new since last run".
"""
import datetime as _dt
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _auth import get_publication_id, get_token
from _graphql import gql
from _me import get_username
from _outbound import my_comment_ids, read as read_outbound, unique_post_ids

STATE_FILE = Path(os.path.expanduser("~/.config/hashnode/last_check"))
DEFAULT_LOOKBACK_HOURS = 24

QUERY = """
query Status($publicationId: ObjectId!, $postFirst: Int!, $cFirst: Int!) {
  publication(id: $publicationId) {
    title
    followersCount
    posts(first: $postFirst) {
      edges { node {
        id title url publishedAt reactionCount responseCount
        comments(first: $cFirst) {
          edges { node {
            id dateAdded
            content { markdown }
            author { username }
          } }
        }
      } }
    }
  }
}
"""


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_state() -> str | None:
    try:
        return STATE_FILE.read_text().strip() or None
    except FileNotFoundError:
        return None


def _write_state(value: str) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(value + "\n")


def resolve_since(arg: str) -> str:
    if arg:
        return arg
    stored = _read_state()
    if stored:
        return stored
    fallback = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=DEFAULT_LOOKBACK_HOURS)
    return fallback.strftime("%Y-%m-%dT%H:%M:%SZ")


def filter_recent(post: dict, since: str, max_per_post: int, me: str = "") -> list[dict]:
    edges = (post.get("comments") or {}).get("edges", [])
    nodes = [e["node"] for e in edges if (e["node"].get("dateAdded") or "") > since]
    if me:
        nodes = [n for n in nodes if (n.get("author") or {}).get("username") != me]
    return nodes[:max_per_post]


def count_my_recent(post: dict, since: str, me: str) -> int:
    if not me:
        return 0
    edges = (post.get("comments") or {}).get("edges", [])
    return sum(
        1 for e in edges
        if (e["node"].get("dateAdded") or "") > since
        and (e["node"].get("author") or {}).get("username") == me
    )


CROSS_POST_QUERY = """
query CrossPostReplies($id: ID!, $cFirst: Int!, $rFirst: Int!) {
  post(id: $id) {
    id title url
    comments(first: $cFirst) {
      edges { node {
        id
        replies(first: $rFirst) {
          edges { node {
            id dateAdded content { markdown } author { username }
          } }
        }
      } }
    }
  }
}
"""


def find_replies_on_post(post: dict, my_ids: set[str], since: str) -> list[dict]:
    out: list[dict] = []
    for ce in (post.get("comments") or {}).get("edges", []):
        cnode = ce["node"]
        if cnode["id"] not in my_ids:
            continue
        for re_e in (cnode.get("replies") or {}).get("edges", []):
            rnode = re_e["node"]
            if (rnode.get("dateAdded") or "") > since:
                out.append(rnode)
    return out


def render(pub: dict, since: str, now: str, me: str = "",
           replies_to_me: list[tuple[dict, dict]] | None = None) -> str:
    posts = [e["node"] for e in ((pub.get("posts") or {}).get("edges") or [])]
    new_comments: list[tuple[dict, dict]] = []
    my_recent = 0
    for p in posts:
        for c in filter_recent(p, since, 10, me):
            new_comments.append((p, c))
        my_recent += count_my_recent(p, since, me)
    out = [
        f"=== Hashnode @{pub.get('title','?')} since {since} (now {now}) ===",
        f"FOLLOWERS: {pub.get('followersCount', 0)}",
        f"MY ENGAGEMENT: {my_recent} comments by you in this window",
    ]
    if replies_to_me:
        out.append(f"REPLIES TO YOUR COMMENTS ({len(replies_to_me)}):")
        for p, r in replies_to_me:
            au = (r.get("author") or {}).get("username", "?")
            rdate = (r.get("dateAdded") or "").split("T")[0]
            rid = r.get("id", "?")
            txt = ((r.get("content") or {}).get("markdown") or "").replace("\n", " ")[:200]
            out.append(f"  [reply {rid}] on {p.get('title','?')!r} ({p.get('url','')})")
            out.append(f"    {rdate} @{au}: {txt}")
            out.append(f"    NEXT: hashnode_reply:{rid}|MSG  — reply back")
    if new_comments:
        out.append(f"NEW COMMENTS ({len(new_comments)}):")
        for p, c in new_comments:
            au = (c.get("author") or {}).get("username", "?")
            cdate = (c.get("dateAdded") or "").split("T")[0]
            txt = ((c.get("content") or {}).get("markdown") or "").replace("\n", " ")[:160]
            out.append(f"  [comment {c['id']}] on {p['title']!r} ({p['url']})")
            out.append(f"    {cdate} @{au}: {txt}")
            out.append(f"    NEXT: hashnode_reply:{c['id']}|MSG  |  hashnode_comment:{p['id']}|MSG")
    else:
        out.append("NEW COMMENTS: (none)")
    top = sorted(posts, key=lambda p: (p.get("reactionCount", 0), p.get("responseCount", 0)), reverse=True)[:3]
    if top:
        out.append("TOP POSTS (by engagement):")
        for p in top:
            out.append(
                f"  - {p['title']!r}: {p.get('reactionCount',0)} reactions, "
                f"{p.get('responseCount',0)} comments → {p['url']}"
            )
    out.append("--- NEXT ---")
    if new_comments:
        out.append("  Reply to fresh comments above (hashnode_reply:COMMENT_ID|MSG)")
    out.append("  hashnode_browse:ai:5     — see what's hot in your tags")
    out.append("  hashnode_search:QUERY    — find related posts to engage with")
    out.append("  hashnode_read:SLUG       — read any post + comments + NEXT")
    return "\n".join(out)


def main(arg: str) -> None:
    since = resolve_since(arg)
    now = _now_iso()
    token = get_token()
    pub_id = get_publication_id()
    post_first = int(os.environ.get("SUPERTOOL_STATUS_POSTS", "10"))
    c_first = int(os.environ.get("SUPERTOOL_STATUS_COMMENTS", "20"))
    data = gql(QUERY, {"publicationId": pub_id, "postFirst": post_first, "cFirst": c_first}, token)
    pub = data.get("publication") or {}
    me = get_username(token)

    # Cross-post reply scan via outbound ledger
    outbound = read_outbound()
    my_ids = my_comment_ids(outbound)
    own_post_ids = {e["node"]["id"] for e in ((pub.get("posts") or {}).get("edges") or [])}
    extra_post_ids = [pid for pid in unique_post_ids(outbound) if pid not in own_post_ids]
    replies_to_me: list[tuple[dict, dict]] = []
    for ext_pid in extra_post_ids:
        cp = gql(CROSS_POST_QUERY, {"id": ext_pid, "cFirst": 50, "rFirst": 20}, token)
        post = cp.get("post")
        if not post:
            continue
        for r in find_replies_on_post(post, my_ids, since):
            replies_to_me.append((post, r))

    print(render(pub, since, now, me, replies_to_me))
    _write_state(now)


if __name__ == "__main__":
    # Supertool splits args on ':' so an ISO timestamp arrives as multiple argv;
    # rejoin everything after argv[0] with ':' to reconstruct.
    arg = ":".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    main(arg)
