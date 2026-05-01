"""Bluesky AT Protocol XRPC helpers. Stdlib-only.

Auth flow:
  1. createSession with handle + app password → accessJwt (~2h) + refreshJwt
  2. Cache session JSON to ~/.config/bluesky/session.json (chmod 600)
  3. Subsequent calls use the cached accessJwt
  4. On 401, refresh via refreshJwt; on refresh failure, recreate from app password
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

PDS = "https://bsky.social"  # Default PDS — handles can override via DID resolution
SESSION_FILE = Path(os.path.expanduser("~/.config/bluesky/session.json"))


def _format_http_error(e: urllib.error.HTTPError) -> str:
    body = e.read().decode("utf-8", errors="replace")
    if e.code == 400:
        return f"400 Bad Request: {body[:200]}"
    if e.code == 401:
        return "401 Unauthorized — session expired or app password wrong. Try deleting ~/.config/bluesky/session.json and retrying."
    if e.code == 403:
        return f"403 Forbidden: {body[:200]}"
    if e.code == 404:
        return f"404 Not Found: {body[:200]}"
    if e.code == 429:
        return "429 Rate Limited — back off and retry"
    return f"HTTP {e.code} {e.reason}: {body[:200]}"


def _save_session(session: dict[str, Any]) -> None:
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps(session))
    try:
        os.chmod(SESSION_FILE, 0o600)
    except OSError:
        pass


def _load_session() -> dict[str, Any] | None:
    try:
        return json.loads(SESSION_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def create_session(handle: str, app_password: str) -> dict[str, Any]:
    payload = json.dumps({"identifier": handle, "password": app_password}).encode("utf-8")
    req = urllib.request.Request(
        f"{PDS}/xrpc/com.atproto.server.createSession",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            session = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"ERROR: createSession: {_format_http_error(e)}\n")
        sys.exit(1)
    except urllib.error.URLError as e:
        sys.stderr.write(f"ERROR: network: {e.reason}\n")
        sys.exit(1)
    session["_created_at"] = int(time.time())
    _save_session(session)
    return session


def refresh_session(refresh_jwt: str) -> dict[str, Any] | None:
    req = urllib.request.Request(
        f"{PDS}/xrpc/com.atproto.server.refreshSession",
        headers={"Authorization": f"Bearer {refresh_jwt}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            session = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError:
        return None
    except urllib.error.URLError:
        return None
    session["_created_at"] = int(time.time())
    _save_session(session)
    return session


def get_session(handle: str, app_password: str) -> dict[str, Any]:
    """Return a valid session, creating/refreshing as needed."""
    session = _load_session()
    if session and session.get("handle") == handle:
        # Token lifetimes are ~2 hours; refresh if older than 1.5h
        age = int(time.time()) - session.get("_created_at", 0)
        if age < 5400:
            return session
        refreshed = refresh_session(session.get("refreshJwt", ""))
        if refreshed:
            return refreshed
    return create_session(handle, app_password)


def xrpc(
    nsid: str,
    session: dict[str, Any],
    method: str = "GET",
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """Call an XRPC endpoint. NSID is the namespaced procedure id (e.g. app.bsky.feed.searchPosts)."""
    url = f"{PDS}/xrpc/{nsid}"
    if params:
        clean = {k: v for k, v in params.items() if v is not None and v != ""}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Authorization": f"Bearer {session['accessJwt']}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"ERROR: {nsid}: {_format_http_error(e)}\n")
        sys.exit(1)
    except urllib.error.URLError as e:
        sys.stderr.write(f"ERROR: network: {e.reason}\n")
        sys.exit(1)
