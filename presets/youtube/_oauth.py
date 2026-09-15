"""YouTube OAuth2 for the write ops (#227), stdlib only.

The read ops authenticate with an API key (`_auth.get_api_key`). A key cannot
write: `commentThreads.insert`, `comments.insert` and `videos.rate` all act as
a *user*, so they need an OAuth2 access token carrying the
`https://www.googleapis.com/auth/youtube.force-ssl` scope.

No vendored `google-auth`: this repo is stdlib-only (CLAUDE.md), so the flow
is implemented directly against Google's OAuth2 endpoints on top of
`presets/_http.py`'s opener, the same way `_yt.py` calls the Data API rather
than the client library.

## The flow, and why it is a separate op

`youtube_auth` runs the one-time authorisation: it starts a loopback HTTP
server on 127.0.0.1, opens a browser, and exchanges the returned code for a
refresh token. `youtube_comment` never runs it. A write op that can silently
open a browser and block on a human is a write op that hangs a non-interactive
session with no output, and the failure looks identical to a slow API -- so an
absent token is a refusal here, naming `youtube_auth`, not a prompt.

## PKCE is not optional here

A "Desktop app" OAuth client's secret ships on the user's machine and is not
a secret in the sense the name implies; Google's own guidance for installed
apps is PKCE (RFC 7636), and the loopback redirect is reachable by any local
process. The verifier is generated per run with `secrets.token_urlsafe` and
never written to disk. `state` is checked on the way back for the same reason:
without it any page the user visits can hit the loopback port with a code.

## What is stored

`~/.config/youtube/oauth_token.json`, 0600, holding the refresh token, the
last access token and its expiry. The access token is short-lived (~1h) and
refreshed in place; the refresh token is the credential that matters and is
what `check_token_file_mode` guards. Nothing here is ever printed: the token
file path is reported, the token is not.

**The 0600 is a POSIX claim and only a POSIX claim.** On a filesystem that
does not enforce the mode bits -- Windows, where CPython synthesises `0o666`
for every file and `os.chmod` cannot change it -- the mode says nothing about
who can read this, and what protects it is the user profile's ACL, which this
tool can neither read nor set with the standard library. `check_token_file_mode`
says so on stderr there rather than passing silently or refusing outright
(#227); the refusal it used to give would have made every write op on Windows
exit 2 blaming the operator's permissions.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))  # for _http, _publish_safety

from _http import (  # noqa: E402
    ERROR_BODY_BYTES,
    DeadlineExceeded,
    RedirectRefused,
    ResponseTooLarge,
    read_capped,
    urlopen,
)
from _publish_safety import check_token_file_mode  # noqa: E402

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"

CONFIG_DIR = Path(os.path.expanduser("~/.config/youtube"))
CLIENT_SECRET_PATH = CONFIG_DIR / "client_secret.json"
TOKEN_PATH = CONFIG_DIR / "oauth_token.json"

#: Refresh this many seconds before the recorded expiry. A token that expires
#: mid-request fails the write, not the refresh, and a write is the thing this
#: preset cannot retry safely.
_EXPIRY_SKEW_S = 120

_BROWSER_WAIT_S = 300


class OAuthError(Exception):
    """Any failure obtaining a usable access token.

    Raised rather than `sys.exit`ing so a caller can distinguish "no
    credentials" from "the API refused the write", which are different
    sentences to the operator and different next commands.
    """


def _scrub(s: str, *secret_values: Optional[str]) -> str:
    """Remove every token-shaped value from a string bound for stderr.

    Google's token endpoint echoes the request on some errors, and a refresh
    token in a traceback is a durable credential in a log file. Mirrors
    `_yt._scrub`, which does the same for the API key.
    """
    for value in secret_values:
        if value and len(value) >= 8:
            s = s.replace(value, "[REDACTED]")
    return s


def _post_form(url: str, fields: dict, *, timeout: int = 30) -> dict:
    """POST an x-www-form-urlencoded body and parse the JSON response."""
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    secret_values = (fields.get("client_secret"), fields.get("refresh_token"),
                     fields.get("code"), fields.get("code_verifier"))
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = read_capped(resp).decode("utf-8")
    except urllib.error.HTTPError as e:
        body = e.read(ERROR_BODY_BYTES).decode("utf-8", errors="replace")
        raise OAuthError(_scrub(f"HTTP {e.code} from the token endpoint: "
                                f"{body[:300]}", *secret_values)) from e
    except (RedirectRefused, ResponseTooLarge, DeadlineExceeded) as e:
        raise OAuthError(_scrub(str(e), *secret_values)) from e
    except urllib.error.URLError as e:
        raise OAuthError(f"network error reaching the token endpoint: {e.reason}") from e
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise OAuthError(f"token endpoint response did not parse as JSON: {e}") from e


def load_client_secret() -> tuple[str, str]:
    """(client_id, client_secret) from `~/.config/youtube/client_secret.json`.

    Accepts either the `installed` or the `web` wrapper Google writes, because
    which one you get depends on the client type picked in the console and the
    file is otherwise identical in the fields used here.
    """
    if not CLIENT_SECRET_PATH.is_file():
        raise OAuthError(
            f"no OAuth client secret at {CLIENT_SECRET_PATH}.\n"
            "  Create one at https://console.cloud.google.com/apis/credentials\n"
            "  (Create credentials -> OAuth client ID -> Desktop app), download\n"
            f"  the JSON, and save it there. The YouTube Data API v3 must be\n"
            "  enabled on the same project."
        )
    check_token_file_mode(CLIENT_SECRET_PATH)
    try:
        blob = json.loads(CLIENT_SECRET_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise OAuthError(f"could not read {CLIENT_SECRET_PATH}: {e}") from e
    node = blob.get("installed") or blob.get("web") or {}
    client_id = node.get("client_id")
    client_secret = node.get("client_secret")
    if not client_id or not client_secret:
        raise OAuthError(
            f"{CLIENT_SECRET_PATH} has no installed.client_id / web.client_id -- "
            "this does not look like a Google OAuth client secret file."
        )
    return client_id, client_secret


def _read_token_file() -> Optional[dict]:
    if not TOKEN_PATH.is_file():
        return None
    check_token_file_mode(TOKEN_PATH)
    try:
        return json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise OAuthError(
            f"could not read the token cache at {TOKEN_PATH}: {e}.\n"
            "  Delete it and run youtube_auth again."
        ) from e


def _write_token_file(payload: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = TOKEN_PATH.with_suffix(".json.tmp")
    # 0600 BEFORE any bytes land. Writing then chmod-ing leaves a window in
    # which a world-readable file holds a refresh token, and on a shared
    # machine that window is the whole vulnerability.
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(str(tmp), str(TOKEN_PATH))


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Single-shot loopback receiver for the authorisation code."""

    result: dict = {}

    def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's name
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        type(self).result = {k: v[0] for k, v in query.items()}
        ok = "code" in type(self).result
        body = (b"<html><body><h1>youtube_auth</h1><p>"
                + (b"Authorised. Close this tab and return to the terminal."
                   if ok else
                   b"No authorisation code came back. Return to the terminal.")
                + b"</p></body></html>")
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        """Silence. The default handler logs every request to stderr, and the
        request line here carries the authorisation code."""


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def authorize(*, open_browser: bool = True) -> dict:
    """Run the one-time browser flow and persist a refresh token.

    Returns the stored payload. Raises `OAuthError` with a sentence naming the
    next command on every failure path -- an expired consent screen, a denied
    scope and a closed browser all arrive here as "no code", and telling them
    apart is not possible from this side.
    """
    client_id, client_secret = load_client_secret()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    state = secrets.token_urlsafe(24)
    port = _free_loopback_port()
    redirect_uri = f"http://127.0.0.1:{port}"

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "access_type": "offline",
        # Without this Google returns a refresh token only on the FIRST
        # consent for a client, so a re-auth after deleting the cache comes
        # back with an access token and nothing to refresh with -- which
        # fails an hour later, far from the command that caused it.
        "prompt": "consent",
    }
    url = f"{AUTH_URI}?{urllib.parse.urlencode(params)}"

    _CallbackHandler.result = {}
    server = http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.timeout = _BROWSER_WAIT_S
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    sys.stderr.write(f"Open this URL to authorise (listening on {redirect_uri}):\n{url}\n")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 -- a headless box has no browser
            pass

    thread.join(timeout=_BROWSER_WAIT_S + 5)
    server.server_close()
    result = _CallbackHandler.result
    _CallbackHandler.result = {}

    if not result:
        raise OAuthError(
            f"no callback arrived within {_BROWSER_WAIT_S}s. Nothing was stored. "
            "Open the URL above by hand and run youtube_auth again."
        )
    if result.get("state") != state:
        raise OAuthError(
            "the callback's `state` did not match the one sent. Nothing was "
            "stored. This is what a cross-site request to the loopback port "
            "looks like; re-run youtube_auth and use only the URL it prints."
        )
    if "code" not in result:
        raise OAuthError(
            "the callback carried no authorisation code"
            + (f" (error={result['error']!r})" if "error" in result else "")
            + ". Nothing was stored."
        )

    tokens = _post_form(TOKEN_URI, {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": result["code"],
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    })
    refresh = tokens.get("refresh_token")
    if not refresh:
        raise OAuthError(
            "Google returned no refresh_token, so the next command would have "
            "to authorise again. Revoke this app at "
            "https://myaccount.google.com/permissions and re-run youtube_auth."
        )
    payload = {
        "client_id": client_id,
        "refresh_token": refresh,
        "access_token": tokens.get("access_token", ""),
        "expires_at": time.time() + float(tokens.get("expires_in", 0) or 0),
        "scope": tokens.get("scope", SCOPE),
    }
    _write_token_file(payload)
    return payload


def get_access_token() -> str:
    """A live access token, refreshing if needed. Never runs the browser flow.

    An absent or unusable cache is an `OAuthError` naming `youtube_auth`, not
    a prompt: see this module's docstring for why a write op must not be the
    thing that opens a browser.
    """
    cached = _read_token_file()
    if not cached or not cached.get("refresh_token"):
        raise OAuthError(
            f"no YouTube OAuth token at {TOKEN_PATH}. Write ops need one.\n"
            "  Run:  supertool 'youtube_auth'"
        )
    token = cached.get("access_token")
    expires_at = float(cached.get("expires_at") or 0)
    if token and expires_at - _EXPIRY_SKEW_S > time.time():
        return str(token)

    _client_id, client_secret = load_client_secret()
    fresh = _post_form(TOKEN_URI, {
        "client_id": cached.get("client_id", ""),
        "client_secret": client_secret,
        "refresh_token": cached["refresh_token"],
        "grant_type": "refresh_token",
    })
    access = fresh.get("access_token")
    if not access:
        raise OAuthError(
            "the refresh succeeded but returned no access_token. The grant was "
            "probably revoked; run youtube_auth again."
        )
    cached["access_token"] = access
    cached["expires_at"] = time.time() + float(fresh.get("expires_in", 0) or 0)
    _write_token_file(cached)
    return str(access)
