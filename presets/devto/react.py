#!/usr/bin/env python3
"""Dev.to react: devto_react:ARTICLE_ID_OR_SLUG_OR_URL[|CATEGORY[|toggle]]

Category: like (default), unicorn, readinglist, thumbsdown, vomit.

By default the op is IDEMPOTENT — it always ends with the reaction present
(result=create). If the first POST returns result=destroy (reaction was already
set and got toggled off), a second POST is issued immediately to restore it,
and the output line is annotated with "was_on=true".

Pass `:toggle` as the third pipe-separated field to get the raw toggle
behaviour (one POST, no correction):
  devto_react:author/slug|like|toggle

Accepts numeric article ID, bare slug (e.g. my-post-3j3e), author/slug, or
full URL — non-numeric inputs are resolved to the numeric ID via the Dev.to
API before posting. The Reactions endpoint requires the integer ID; passing a
slug raw returns 422 "Reactable not valid".

Two auth modes:

  1. SESSION (preferred): set DEVTO_SESSION_COOKIE (or write
     ~/.config/devto/session_cookie). The op POSTs to /reactions on
     the web endpoint with the session + CSRF token scraped from
     /settings. EXPERIMENTAL — may violate Dev.to ToS, see _session.py.
  2. API: falls back to /api/reactions/toggle. Currently fails with
     401 because Dev.to API keys do not authorize reactions; kept as
     placeholder until the platform exposes a proper write scope.
"""
import json as _json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _auth import get_api_key
from _resolve import resolve_article_id
from _rest import request
from _session import fetch_csrf_token, get_session_cookie, web_post_json

VALID = {"like", "unicorn", "readinglist", "thumbsdown", "vomit"}


def parse_args(arg: str) -> tuple[str, str, bool]:
    """Return (raw_identifier, category, idempotent).

    idempotent=True (default) means the op ensures the reaction ends as
    'create'. idempotent=False (pass '|toggle' as third field) means one
    raw POST with no correction.
    """
    if not arg:
        sys.stderr.write("ERROR: usage devto_react:ARTICLE_ID_OR_SLUG_OR_URL[|CATEGORY[|toggle]]\n")
        sys.exit(2)
    parts = arg.split("|")
    aid = parts[0].strip()
    category = parts[1].strip().lower() if len(parts) > 1 and parts[1].strip() else "like"
    if category not in VALID:
        sys.stderr.write(f"ERROR: category must be one of {sorted(VALID)}\n")
        sys.exit(2)
    toggle_mode = len(parts) > 2 and parts[2].strip().lower() == "toggle"
    return aid, category, not toggle_mode


def _react_once_session(aid: int, category: str, cookie: str, csrf: str) -> str:
    """POST one reaction via session cookie. Returns result string ('create'/'destroy'/'unknown')."""
    body = {"reactable_id": aid, "reactable_type": "Article", "category": category}
    text, _status = web_post_json("/reactions", cookie, csrf, body)
    try:
        data = _json.loads(text)
    except _json.JSONDecodeError:
        data = {}
    return data.get("result", "unknown")


def main(arg: str) -> None:
    raw, category, idempotent = parse_args(arg)
    aid = resolve_article_id(raw)
    cookie = get_session_cookie()
    if cookie:
        csrf = fetch_csrf_token(cookie)
        result = _react_once_session(aid, category, cookie, csrf)
        was_on = False
        if idempotent and result == "destroy":
            was_on = True
            sys.stderr.write(
                f"NOTE: reaction was already set — toggled off then back on (idempotent mode). "
                f"Use '|{category}|toggle' to suppress this.\n"
            )
            result = _react_once_session(aid, category, cookie, csrf)
        suffix = " was_on=true" if was_on else ""
        print(f"(react article={aid} category={category} result={result} mode=session{suffix})")
        return
    # Fallback: API key (currently 401 for reactions)
    api_key = get_api_key()
    body = {"reactable_id": aid, "reactable_type": "Article", "category": category}
    data = request("POST", "/reactions/toggle", api_key, body=body)
    result = data.get("result") if isinstance(data, dict) else None
    was_on = False
    if idempotent and result == "destroy":
        was_on = True
        sys.stderr.write(
            f"NOTE: reaction was already set — toggled off then back on (idempotent mode). "
            f"Use '|{category}|toggle' to suppress this.\n"
        )
        data = request("POST", "/reactions/toggle", api_key, body=body)
        result = data.get("result") if isinstance(data, dict) else None
    suffix = " was_on=true" if was_on else ""
    print(f"(react article={aid} category={category} result={result or 'unknown'} mode=api{suffix})")


if __name__ == "__main__":
    # Supertool {args} splits on ':' — URLs like https://... arrive as multiple
    # argv parts. Rejoin so the full identifier survives the tokenizer.
    arg = ":".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    main(arg)
