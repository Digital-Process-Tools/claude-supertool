#!/usr/bin/env python3
"""Dev.to comments: devto_comments:ARTICLE_ID[:N]"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _auth import get_api_key
from _outbound import my_comment_ids, read as read_outbound
from _rest import request
from _sanitize import safe_short


def parse_args(arg: str) -> tuple[str, int]:
    if not arg:
        sys.stderr.write("ERROR: usage devto_comments:ARTICLE_ID[:N]\n")
        sys.exit(2)
    default_n = int(os.environ.get("SUPERTOOL_DEFAULT_LIMIT", "20"))
    parts = arg.split(":")
    aid = parts[0]
    n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else default_n
    return aid, n


def _flatten(comments: list[dict], depth: int = 0) -> list[tuple[int, dict]]:
    out: list[tuple[int, dict]] = []
    for c in comments:
        out.append((depth, c))
        children = c.get("children") or []
        if children:
            out.extend(_flatten(children, depth + 1))
    return out


def render(aid: str, comments: list[dict], limit: int,
           mine: set[str] | None = None) -> str:
    mine = mine or set()
    flat = _flatten(comments)[:limit]
    if not flat:
        return f"(0 comments on article {aid})"
    out = [f"({len(flat)} comments on article {aid})"]
    for depth, c in flat:
        user = c.get("user") or {}
        date = (c.get("created_at") or "").split("T")[0]
        raw_body = (c.get("body_html") or "").replace("<p>", "").replace("</p>", " ")
        body = safe_short(raw_body, 300)
        prefix = "  " * depth + ("- " if depth == 0 else "↳ ")
        cid = c.get("id_code") or ""
        you = "[YOU] " if cid and cid in mine else ""
        username = safe_short(user.get("username") or "?", 60)
        out.append(f"{prefix}{you}[id={cid}] {date} @{username}: {body}")
    return "\n".join(out)


def main(arg: str) -> None:
    aid, n = parse_args(arg)
    api_key = get_api_key()
    # Dev.to /comments supports per_page; bump to grab full thread in one shot.
    items = request("GET", "/comments", api_key, query={"a_id": aid, "per_page": 1000})
    if not isinstance(items, list):
        items = []
    mine = my_comment_ids(read_outbound())
    print(render(aid, items, n, mine))


if __name__ == "__main__":
    # Supertool {args} passes parts space-separated; rejoin with ':' to reconstruct
    # the canonical 'aid[:N]' shape parse_args expects.
    arg = ":".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    main(arg)
