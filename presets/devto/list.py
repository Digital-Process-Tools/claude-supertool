#!/usr/bin/env python3
"""Dev.to list: devto_list[:N] (own published) | devto_list:USER[:N]

N defaults to SUPERTOOL_DEFAULT_LIMIT or 10.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _auth import get_api_key
from _rest import request


def parse_args(arg: str) -> tuple[str | None, int]:
    default_n = int(os.environ.get("SUPERTOOL_DEFAULT_LIMIT", "10"))
    if not arg or arg.isdigit():
        return None, int(arg) if arg else default_n
    parts = arg.split(":")
    user = parts[0]
    n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else default_n
    return user, n


def render(items: list[dict]) -> str:
    if not items:
        return "(no articles)"
    out = [f"({len(items)} articles)"]
    for a in items:
        date = (a.get("published_at") or "").split("T")[0]
        out.append(
            f"- {date} {a.get('title','?')!r} → {a.get('url','?')} "
            f"({a.get('public_reactions_count', 0)} reactions, {a.get('comments_count', 0)} comments)"
        )
    return "\n".join(out)


def main(arg: str) -> None:
    user, n = parse_args(arg)
    api_key = get_api_key()
    if user is None:
        items = request("GET", "/articles/me/published", api_key, query={"per_page": n})
    else:
        items = request("GET", "/articles", api_key, query={"username": user, "per_page": n})
    if not isinstance(items, list):
        items = []
    print(render(items))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
