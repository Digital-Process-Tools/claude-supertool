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
from __future__ import annotations

import http.client
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # for _http (#691)

from _http import (  # noqa: E402
    ERROR_BODY_BYTES,
    DeadlineExceeded,
    RedirectRefused,
    ResponseTooLarge,
    read_capped,
    urlopen,
)

WEB_BASE = "https://dev.to"


def get_session_cookie() -> str | None:
    val = os.environ.get("DEVTO_SESSION_COOKIE", "").strip()
    if val:
        return val
    p = Path(os.path.expanduser("~/.config/devto/session_cookie"))
    if p.is_file():
        return p.read_text(encoding="utf-8").strip() or None
    return None


_AUTH_TOKEN_RE = re.compile(r'name="authenticity_token"[^>]*value="([^"]+)"')


def fetch_csrf_token(cookie: str, timeout: int = 15) -> str:
    """Scrape a real authenticity_token from /settings (server-renders Rails forms).

    Three outcomes, not two (#766). A missing token used to produce one message
    -- "Dev.to layout may have changed" -- for a condition this function had
    never checked, and the overwhelmingly common cause is the other one: dev.to
    answers /settings with a same-origin 302 to /enter once the session cookie
    expires, `_http.urlopen` follows it, and the login page has no
    authenticity_token in it. So the token is absent either because the page
    that answered was not the page asked for (the cookie is dead, and
    `resp.url` says so), or because /settings itself answered without one --
    which is genuinely unexplained and keeps saying so rather than being
    reassigned to the cookie. Guessing in the other direction is the same
    defect facing the other way.
    """
    requested = f"{WEB_BASE}/settings"
    req = urllib.request.Request(
        requested,
        headers={
            "Cookie": cookie,
            "User-Agent": "Mozilla/5.0 (claude-supertool/devto-session)",
            "Accept": "text/html",
        },
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            answered = getattr(resp, "url", None) or requested
            html = read_capped(resp).decode("utf-8", errors="replace")
    except RedirectRefused as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except (ResponseTooLarge, DeadlineExceeded) as e:
        # This was the worst of the unbounded call sites: it decoded the whole
        # body to run one regex over it (#766).
        sys.stderr.write(f"ERROR: {e}\n")
        sys.exit(1)
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"ERROR: session-cookie fetch failed: HTTP {e.code} (cookie expired?)\n")
        sys.exit(1)
    except http.client.HTTPException as e:
        sys.stderr.write(f"ERROR: incomplete response: {type(e).__name__}: {e}\n")
        sys.exit(1)
    tokens = [t for t in _AUTH_TOKEN_RE.findall(html) if t != "NOTHING"]
    if not tokens:
        if answered != requested:
            # `answered` comes from a remote Location header — `!r` for the same
            # reason `_http` reprs both ends of its redirect disclosure.
            sys.stderr.write(
                f"ERROR: no authenticity_token in the response: {requested!r} was answered by "
                f"{answered!r}, a different page than the one requested. On dev.to that is the "
                "expired-session redirect (/settings -> /enter), so the session cookie is dead. "
                "Re-copy it from your browser devtools — they last about 30 days.\n"
            )
        else:
            sys.stderr.write(
                f"ERROR: authenticity_token not found in {requested!r} HTML, which answered the "
                "request itself with no redirect — the session cookie is live, so the cause is "
                "not an expired session. Dev.to layout may have changed.\n"
            )
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
        with urlopen(req, timeout=timeout) as resp:
            return read_capped(resp).decode("utf-8", errors="replace"), resp.status
    except RedirectRefused as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except (ResponseTooLarge, DeadlineExceeded) as e:
        sys.stderr.write(f"ERROR: {e}\n")
        sys.exit(1)
    except http.client.HTTPException as e:
        sys.stderr.write(f"ERROR: incomplete response: {type(e).__name__}: {e}\n")
        sys.exit(1)
    except urllib.error.HTTPError as e:
        text = e.read(ERROR_BODY_BYTES).decode("utf-8", errors="replace")[:300]
        if e.code in (401, 403):
            sys.stderr.write("ERROR: session unauthorized — cookie expired/rotated. Re-copy from browser.\n")
        elif e.code == 422:
            sys.stderr.write(f"ERROR: 422 — likely CSRF or body shape mismatch: {text}\n")
        else:
            sys.stderr.write(f"ERROR: HTTP {e.code} {e.reason}: {text}\n")
        sys.exit(1)
