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


def filter_recent_comments(comments: list[dict], since: str, max_per_post: int) -> list[dict]:
    flat: list[dict] = []
    for c in comments:
        if (c.get("created_at") or "") > since:
            flat.append(c)
        for child in c.get("children") or []:
            if (child.get("created_at") or "") > since:
                flat.append(child)
    return flat[:max_per_post]


def render(articles: list[dict], comments_by_article: dict[int, list[dict]],
           since: str, now: str) -> str:
    out = [f"=== Dev.to since {since} (now {now}) ==="]
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
    if new_total:
        out.append("  React to fresh comments (devto_react:ARTICLE_ID); comment-write is web-only")
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
    comments_by_article: dict[int, list[dict]] = {}
    cap = int(os.environ.get("SUPERTOOL_STATUS_COMMENTS", "20"))
    for a in articles:
        aid = a.get("id")
        if not aid:
            continue
        c = request("GET", "/comments", api_key, query={"a_id": aid})
        if isinstance(c, list):
            comments_by_article[aid] = filter_recent_comments(c, since, cap)
    print(render(articles, comments_by_article, since, now))
    _write_state(now)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
