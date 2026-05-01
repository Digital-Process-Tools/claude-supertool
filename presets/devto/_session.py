"""Dev.to session-cookie auth (EXPERIMENTAL, opt-in).

API key alone cannot react, comment, or follow. These actions require
a logged-in browser session. This module supports re-using a session
cookie that the user copies from their browser devtools.

LEGAL/ToS: Automating actions through a session cookie may violate
Dev.to's Terms of Service. Risk falls on the user. This module never
auto-creates sessions — you paste your own cookie. We don't bypass
captchas. We don't impersonate. We just re-use what you authorized.

Resolution order for the cookie (first hit wins):
1. DEVTO_SESSION_COOKIE env var (full Cookie header, e.g. `_forem_user=...; remember_user_token=...`)
2. ~/.config/devto/session_cookie (one-line file)

CSRF token is scraped from any /dashboard fetch (which we already do).
Cached for the script run.
"""
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

WEB_BASE = "https://dev.to"


def get_session_cookie() -> str | None:
    val = os.environ.get("DEVTO_SESSION_COOKIE", "").strip()
    if val:
        return val
    p = Path(os.path.expanduser("~/.config/devto/session_cookie"))
    if p.is_file():
        return p.read_text().strip() or None
    return None


_AUTH_TOKEN_RE = re.compile(r'name="authenticity_token"[^>]*value="([^"]+)"')


def fetch_csrf_token(cookie: str, timeout: int = 15) -> str:
    """Scrape a real authenticity_token from /settings (server-renders Rails forms)."""
    req = urllib.request.Request(
        f"{WEB_BASE}/settings",
        headers={
            "Cookie": cookie,
            "User-Agent": "Mozilla/5.0 (claude-supertool/devto-session)",
            "Accept": "text/html",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"ERROR: session-cookie fetch failed: HTTP {e.code} (cookie expired?)\n")
        sys.exit(1)
    tokens = [t for t in _AUTH_TOKEN_RE.findall(html) if t != "NOTHING"]
    if not tokens:
        sys.stderr.write("ERROR: authenticity_token not found in /settings HTML — Dev.to layout may have changed\n")
        sys.exit(1)
    return tokens[0]


def web_post_json(path: str, cookie: str, csrf: str, body: dict, timeout: int = 30):
    """POST JSON body to dev.to web endpoint with session + CSRF. Returns (text, status)."""
    import json as _json
    payload = _json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{WEB_BASE}{path}",
        data=payload,
        headers={
            "Cookie": cookie,
            "User-Agent": "Mozilla/5.0 (claude-supertool/devto-session)",
            "X-CSRF-Token": csrf,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace"), resp.status
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", errors="replace")[:300]
        if e.code in (401, 403):
            sys.stderr.write("ERROR: session unauthorized — cookie expired/rotated. Re-copy from browser.\n")
        elif e.code == 422:
            sys.stderr.write(f"ERROR: 422 — likely CSRF or body shape mismatch: {text}\n")
        else:
            sys.stderr.write(f"ERROR: HTTP {e.code} {e.reason}: {text}\n")
        sys.exit(1)
