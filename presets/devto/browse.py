#!/usr/bin/env python3
"""Dev.to browse: devto_browse:TAG[:N][:SORT]

SORT: recent (default) or top (last 7 days top via ?top=7).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _auth import get_api_key
from _rest import request


SORT_VALUES = {"recent", "top"}


def parse_args(arg: str) -> tuple[str, int, str]:
    if not arg:
        sys.stderr.write("ERROR: usage devto_browse:TAG[:N][:SORT]\n")
        sys.exit(2)
    parts = arg.split(":")
    tag = parts[0]
    default_n = int(os.environ.get("SUPERTOOL_DEFAULT_LIMIT", "10"))
    n = default_n
    sort = "recent"
    for p in parts[1:]:
        if p.isdigit():
            n = int(p)
        elif p in SORT_VALUES:
            sort = p
        else:
            sys.stderr.write(f"ERROR: unknown sort/limit token {p!r}; use a number or one of {sorted(SORT_VALUES)}\n")
            sys.exit(2)
    return tag, n, sort


def render(tag: str, items: list[dict], sort: str = "recent") -> str:
    if not items:
        return f"(no articles on tag {tag})"
    out = [f"({len(items)} articles on tag {tag}, sort={sort})"]
    for a in items:
        user = a.get("user") or {}
        date = (a.get("published_at") or "").split("T")[0]
        out.append(
            f"- {date} {a.get('title','?')!r} by @{user.get('username','?')} → {a.get('url','?')} "
            f"({a.get('public_reactions_count', 0)} reactions, {a.get('comments_count', 0)} comments)"
        )
    return "\n".join(out)


def main(arg: str) -> None:
    tag, n, sort = parse_args(arg)
    api_key = get_api_key()
    query: dict[str, object] = {"tag": tag, "per_page": n}
    if sort == "top":
        query["top"] = 7
    items = request("GET", "/articles", api_key, query=query)
    if not isinstance(items, list):
        items = []
    print(render(tag, items, sort))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
