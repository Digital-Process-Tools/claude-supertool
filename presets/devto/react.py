#!/usr/bin/env python3
"""Dev.to react: devto_react:ARTICLE_ID[|CATEGORY]

Category: like (default), unicorn, readinglist. Toggles on/off.
Note: Dev.to reaction API has limited public support — may fail with auth-only
key (cookie required for some flows).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _auth import get_api_key
from _rest import request

VALID = {"like", "unicorn", "readinglist", "thumbsdown", "vomit"}


def parse_args(arg: str) -> tuple[str, str]:
    if not arg:
        sys.stderr.write("ERROR: usage devto_react:ARTICLE_ID[|CATEGORY]\n")
        sys.exit(2)
    parts = arg.split("|")
    aid = parts[0].strip()
    category = parts[1].strip().lower() if len(parts) > 1 and parts[1].strip() else "like"
    if category not in VALID:
        sys.stderr.write(f"ERROR: category must be one of {sorted(VALID)}\n")
        sys.exit(2)
    return aid, category


def main(arg: str) -> None:
    aid, category = parse_args(arg)
    api_key = get_api_key()
    body = {"reactable_id": int(aid) if aid.isdigit() else aid,
            "reactable_type": "Article",
            "category": category}
    data = request("POST", "/reactions/toggle", api_key, body=body)
    result = data.get("result") if isinstance(data, dict) else None
    print(f"(react article={aid} category={category} result={result or 'unknown'})")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
