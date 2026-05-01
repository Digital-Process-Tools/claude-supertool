#!/usr/bin/env python3
"""Dev.to comments: devto_comments:ARTICLE_ID[:N]"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _auth import get_api_key
from _rest import request


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


def render(aid: str, comments: list[dict], limit: int) -> str:
    flat = _flatten(comments)[:limit]
    if not flat:
        return f"(0 comments on article {aid})"
    out = [f"({len(flat)} comments on article {aid})"]
    for depth, c in flat:
        user = c.get("user") or {}
        date = (c.get("created_at") or "").split("T")[0]
        body = (c.get("body_html") or "").replace("\n", " ").replace("<p>", "").replace("</p>", " ")[:300]
        prefix = "  " * depth + ("- " if depth == 0 else "↳ ")
        out.append(f"{prefix}{date} @{user.get('username','?')}: {body}")
    return "\n".join(out)


def main(arg: str) -> None:
    aid, n = parse_args(arg)
    api_key = get_api_key()
    items = request("GET", "/comments", api_key, query={"a_id": aid})
    if not isinstance(items, list):
        items = []
    print(render(aid, items, n))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
