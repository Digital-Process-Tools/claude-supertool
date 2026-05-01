#!/usr/bin/env python3
"""Dev.to browse: devto_browse:TAG[:N]"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _auth import get_api_key
from _rest import request


def parse_args(arg: str) -> tuple[str, int]:
    if not arg:
        sys.stderr.write("ERROR: usage devto_browse:TAG[:N]\n")
        sys.exit(2)
    parts = arg.split(":")
    tag = parts[0]
    default_n = int(os.environ.get("SUPERTOOL_DEFAULT_LIMIT", "10"))
    n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else default_n
    return tag, n


def render(tag: str, items: list[dict]) -> str:
    if not items:
        return f"(no articles on tag {tag})"
    out = [f"({len(items)} articles on tag {tag})"]
    for a in items:
        user = a.get("user") or {}
        date = (a.get("published_at") or "").split("T")[0]
        out.append(
            f"- {date} {a.get('title','?')!r} by @{user.get('username','?')} → {a.get('url','?')} "
            f"({a.get('public_reactions_count', 0)} reactions, {a.get('comments_count', 0)} comments)"
        )
    return "\n".join(out)


def main(arg: str) -> None:
    tag, n = parse_args(arg)
    api_key = get_api_key()
    items = request("GET", "/articles", api_key, query={"tag": tag, "per_page": n})
    if not isinstance(items, list):
        items = []
    print(render(tag, items))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
