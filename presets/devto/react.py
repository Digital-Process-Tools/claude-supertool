#!/usr/bin/env python3
"""Dev.to react: devto_react:ARTICLE_ID[|CATEGORY]

Category: like (default), unicorn, readinglist, thumbsdown, vomit. Toggles on/off.

Two auth modes:

  1. SESSION (preferred): set DEVTO_SESSION_COOKIE (or write
     ~/.config/devto/session_cookie). The op POSTs to /reactions on
     the web endpoint with the session + CSRF token scraped from
     /settings. EXPERIMENTAL — may violate Dev.to ToS, see _session.py.
  2. API: falls back to /api/reactions/toggle. Currently fails with
     401 because Dev.to API keys do not authorize reactions; kept as
     placeholder until the platform exposes a proper write scope.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _auth import get_api_key
from _rest import request
from _session import fetch_csrf_token, get_session_cookie, web_post_json

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
    cookie = get_session_cookie()
    if cookie:
        csrf = fetch_csrf_token(cookie)
        body = {
            "reactable_id": int(aid) if aid.isdigit() else aid,
            "reactable_type": "Article",
            "category": category,
        }
        text, status = web_post_json("/reactions", cookie, csrf, body)
        import json as _json
        try:
            data = _json.loads(text)
        except _json.JSONDecodeError:
            data = {}
        result = data.get("result", "unknown")
        print(f"(react article={aid} category={category} result={result} mode=session)")
        return
    # Fallback: API key (currently 401 for reactions)
    api_key = get_api_key()
    body = {
        "reactable_id": int(aid) if aid.isdigit() else aid,
        "reactable_type": "Article",
        "category": category,
    }
    data = request("POST", "/reactions/toggle", api_key, body=body)
    result = data.get("result") if isinstance(data, dict) else None
    print(f"(react article={aid} category={category} result={result or 'unknown'} mode=api)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
