#!/usr/bin/env python3
"""Dev.to status_since: devto_status_since[:ISO_TIMESTAMP]

Aggregated activity briefing across own published articles: new
comments + top-engaged posts since the given timestamp. No arg =
uses ~/.config/devto/last_check (auto-tracking).

Fan-out: GET /articles/me/published, then GET /comments?a_id=X for
each recent article (limited by SUPERTOOL_STATUS_POSTS, default 10).
"""
import datetime as _dt
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _auth import get_api_key
from _me import get_username
from _outbound import my_comment_ids, read as read_outbound, unique_article_ids
from _rest import request

STATE_FILE = Path(os.path.expanduser("~/.config/devto/last_check"))
DEFAULT_LOOKBACK_HOURS = 24


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


def filter_recent_comments(comments: list[dict], since: str, max_per_post: int,
                            me: str = "") -> list[dict]:
    flat: list[dict] = []
    for c in comments:
        if (c.get("created_at") or "") > since:
            flat.append(c)
        for child in c.get("children") or []:
            if (child.get("created_at") or "") > since:
                flat.append(child)
    if me:
        flat = [c for c in flat if (c.get("user") or {}).get("username") != me]
    return flat[:max_per_post]


def count_my_recent(comments: list[dict], since: str, me: str) -> int:
    if not me:
        return 0
    n = 0
    for c in comments:
        if (c.get("created_at") or "") > since and (c.get("user") or {}).get("username") == me:
            n += 1
        for child in c.get("children") or []:
            if (child.get("created_at") or "") > since and (child.get("user") or {}).get("username") == me:
                n += 1
    return n


def find_replies_to_me(
    raw_comments: list[dict],
    my_ids: set[str],
    since: str,
) -> list[dict]:
    """Return flat list of replies whose parent.id_code is in my_ids and
    are newer than `since`. Walks one level of children (Dev.to nests one deep).
    """
    out: list[dict] = []
    for top in raw_comments:
        for child in top.get("children") or []:
            parent = top
            parent_cid = parent.get("id_code") or str(parent.get("id", ""))
            if parent_cid in my_ids and (child.get("created_at") or "") > since:
                out.append(child)
    return out


def render(articles: list[dict], comments_by_article: dict[int, list[dict]],
           since: str, now: str, my_recent: int = 0,
           replies_to_me: list[tuple[int | str, str, dict]] | None = None) -> str:
    out = [f"=== Dev.to since {since} (now {now}) ==="]
    out.append(f"MY ENGAGEMENT: {my_recent} comments by you in this window")
    if replies_to_me:
        out.append(f"REPLIES TO YOUR COMMENTS ({len(replies_to_me)}):")
        for aid, atitle, c in replies_to_me:
            au = (c.get("user") or {}).get("username", "?")
            cdate = (c.get("created_at") or "").split("T")[0]
            cid = c.get("id_code") or "?"
            txt = (c.get("body_html") or "").replace("\n", " ").replace("<p>", "").replace("</p>", " ")[:200]
            out.append(f"  [reply {cid}] on {atitle!r} (article {aid})")
            out.append(f"    {cdate} @{au}: {txt}")
            out.append(f"    NEXT: devto_comment:{aid}|MSG|{cid}  — reply back")
    new_total = sum(len(v) for v in comments_by_article.values())
    if new_total:
        out.append(f"NEW COMMENTS ({new_total}):")
        for a in articles:
            aid = a.get("id")
            recents = comments_by_article.get(aid) or []
            for c in recents:
                au = (c.get("user") or {}).get("username", "?")
                cdate = (c.get("created_at") or "").split("T")[0]
                cid = c.get("id_code") or "?"
                txt = (c.get("body_html") or "").replace("\n", " ")[:160]
                out.append(f"  [comment {cid}] on {a.get('title','?')!r} ({a.get('url','')})")
                out.append(f"    {cdate} @{au}: {txt}")
                out.append(f"    NEXT: devto_react:{aid}  (no comment-write API)")
    else:
        out.append("NEW COMMENTS: (none)")
    top = sorted(articles,
                 key=lambda a: (a.get("public_reactions_count", 0), a.get("comments_count", 0)),
                 reverse=True)[:3]
    if top:
        out.append("TOP ARTICLES (by engagement):")
        for a in top:
            out.append(
                f"  - {a.get('title','?')!r}: {a.get('public_reactions_count',0)} reactions, "
                f"{a.get('comments_count',0)} comments → {a.get('url','')}"
            )
    out.append("--- NEXT ---")
    if new_total or replies_to_me:
        out.append("  Reply: devto_comment:ARTICLE|MSG[|PARENT_COMMENT_ID] (session-mode required)")
        out.append("  React: devto_react:ARTICLE  (session-mode required)")
    out.append("  devto_browse:ai:5        — see what's hot in your tags")
    out.append("  devto_read:URL           — read any article + comments + NEXT")
    return "\n".join(out)


def main(arg: str) -> None:
    since = resolve_since(arg)
    now = _now_iso()
    api_key = get_api_key()
    post_n = int(os.environ.get("SUPERTOOL_STATUS_POSTS", "10"))
    articles = request("GET", "/articles/me/published", api_key, query={"per_page": post_n})
    if not isinstance(articles, list):
        articles = []
    me = get_username(api_key)
    comments_by_article: dict[int, list[dict]] = {}
    my_recent = 0
    cap = int(os.environ.get("SUPERTOOL_STATUS_COMMENTS", "20"))
    own_aids = {a.get("id") for a in articles if a.get("id")}
    for a in articles:
        aid = a.get("id")
        if not aid:
            continue
        c = request("GET", "/comments", api_key, query={"a_id": aid})
        if isinstance(c, list):
            comments_by_article[aid] = filter_recent_comments(c, since, cap, me)
            my_recent += count_my_recent(c, since, me)

    # Scan articles where I commented (cross-article reply detection)
    outbound = read_outbound()
    my_ids = my_comment_ids(outbound)
    replies_to_me: list[tuple[int | str, str, dict]] = []
    extra_articles = [a for a in unique_article_ids(outbound) if a not in own_aids]
    for ext_aid in extra_articles:
        c = request("GET", "/comments", api_key, query={"a_id": ext_aid})
        if not isinstance(c, list):
            continue
        # Lookup article title (one extra fetch per article — cheap, cached client-side)
        meta = request("GET", f"/articles/{ext_aid}", api_key)
        title = meta.get("title", "?") if isinstance(meta, dict) else "?"
        for r in find_replies_to_me(c, my_ids, since):
            replies_to_me.append((ext_aid, title, r))

    print(render(articles, comments_by_article, since, now, my_recent, replies_to_me))
    _write_state(now)


if __name__ == "__main__":
    arg = ":".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    main(arg)
