"""Bluesky AT Protocol XRPC helpers. Stdlib-only.

Auth flow:
  1. createSession with handle + app password → accessJwt (~2h) + refreshJwt
  2. Cache session JSON to ~/.config/bluesky/session.json (chmod 600)
  3. Subsequent calls use the cached accessJwt
  4. On 401, refresh via refreshJwt; on refresh failure, recreate from app password
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))  # for _http (#691)

from _http import (  # noqa: E402
    ERROR_BODY_BYTES,
    DeadlineExceeded,
    RedirectRefused,
    ResponseTooLarge,
    read_capped,
    urlopen,
)

PDS = "https://bsky.social"  # Default PDS — handles can override via DID resolution
SESSION_FILE = Path(os.path.expanduser("~/.config/bluesky/session.json"))


def _scrub(s: str, *secrets: str) -> str:
    """Redact credential material from a string before printing it (#691).

    Mirrors `presets/hashnode/_graphql.py::_scrub_token`, which this module
    lacked. The gap mattered most in `create_session`, whose request body *is*
    the app password: an upstream proxy or PDS that echoes the request in its
    error body replayed the password onto stderr and from there into whatever
    read it. Cheap, idempotent, and cheaper than a credential leak.

    Short values are skipped — a 1-2 character "secret" would redact ordinary
    text everywhere it appeared and turn the error message into noise.
    """
    for secret in secrets:
        if secret and len(secret) >= 6:
            s = s.replace(secret, "[REDACTED]")
    return s


def _format_http_error(e: urllib.error.HTTPError, *secrets: str) -> str:
    # Scrubbed before truncation, not after: a secret straddling the 200-char
    # boundary survives `replace()` as a fragment if the order is reversed.
    body = _scrub(e.read(ERROR_BODY_BYTES).decode("utf-8", errors="replace"), *secrets)
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
    """Write the session cache at mode 0o600 from the moment it exists on
    disk -- never at the umask-determined mode `write_text()` + a later
    `chmod()` would leave it at in between the two calls (#2484).
    `os.open()`'s own mode argument is masked by umask the same way a
    plain `open()`'s is, but 0o600 has no group/other bits to mask away,
    so the file is 0o600 under every umask, not just the permissive one
    this fix was found under.

    `os.chmod()`, not `os.fchmod()`, on purpose: `fchmod` does not exist
    on Windows at all (an `AttributeError` `except OSError` never catches),
    where this repo's own CI matrix runs; `os.chmod(path, ...)` is present
    everywhere and, unlike its POSIX behaviour, just toggles the read-only
    attribute there rather than crashing. It stays a *separate*, own
    try/except from the write -- best-effort, narrows a pre-existing file
    left wide by #2484 -- so a narrowing failure never blocks or swallows
    the write itself, matching `write_text()`'s original, unguarded
    propagation of a write failure.
    """
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(SESSION_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.chmod(SESSION_FILE, 0o600)
    except OSError:
        pass
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(session))


def _load_session() -> dict[str, Any] | None:
    try:
        return json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # OSError covers FileNotFoundError + IsADirectoryError + permission
        # errors — all map to "no usable session, force create_session".
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
        with urlopen(req, timeout=20) as resp:
            session = json.loads(read_capped(resp).decode("utf-8"))
    except RedirectRefused as e:
        print(f"ERROR: createSession: {e}", file=sys.stderr)
        sys.exit(1)
    except (ResponseTooLarge, DeadlineExceeded) as e:
        sys.stderr.write(f"ERROR: createSession: {_scrub(str(e), app_password)}\n")
        sys.exit(1)
    except urllib.error.HTTPError as e:
        sys.stderr.write(
            f"ERROR: createSession: {_format_http_error(e, app_password)}\n")
        sys.exit(1)
    except urllib.error.URLError as e:
        sys.stderr.write(f"ERROR: network: {e.reason}\n")
        sys.exit(1)
    except http.client.HTTPException as e:
        sys.stderr.write(
            f"ERROR: createSession: incomplete response: {type(e).__name__}: {e}\n")
        sys.exit(1)
    session["_created_at"] = int(time.time())
    _save_session(session)
    return session


def refresh_session(refresh_jwt: str) -> dict[str, Any] | None:
    """Refresh the cached session, or return None so the caller recreates one.

    The contract, stated once and in full. None is returned for every way the
    refresh can fail as a *request*: an HTTP error status, a network error, a
    response that ends short of its declared length (`IncompleteRead`), a body
    that does not parse, a deadline that runs out. The caller then falls back to
    `create_session`, which is the correct recovery for all of them.

    Two failures are not that, and both exit instead:

    * `RedirectRefused` — the refresh JWT was aimed at another origin (#761).
      The fallback would quietly succeed and hide it.
    * `ResponseTooLarge` — a body over the cap (#766). Falling back would retry
      against the same endpoint and report nothing about why.
    """
    req = urllib.request.Request(
        f"{PDS}/xrpc/com.atproto.server.refreshSession",
        headers={"Authorization": f"Bearer {refresh_jwt}"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=20) as resp:
            session = json.loads(read_capped(resp).decode("utf-8"))
    except (RedirectRefused, ResponseTooLarge) as e:
        print(f"ERROR: refreshSession: {_scrub(str(e), refresh_jwt)}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.HTTPError:
        return None
    except urllib.error.URLError:
        return None
    except OSError:
        # DeadlineExceeded is a TimeoutError: a slow PDS is a refresh that
        # failed, which is exactly what the create_session fallback is for.
        return None
    except (http.client.HTTPException, json.JSONDecodeError):
        # IncompleteRead subclasses HTTPException, not OSError, so it used to
        # propagate out of a function documented as returning None (#766).
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
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(read_capped(resp).decode("utf-8"))
    except RedirectRefused as e:
        print(f"ERROR: {nsid}: {e}", file=sys.stderr)
        sys.exit(1)
    except (ResponseTooLarge, DeadlineExceeded) as e:
        detail = _scrub(str(e), session.get("accessJwt", ""), session.get("refreshJwt", ""))
        sys.stderr.write(f"ERROR: {nsid}: {detail}\n")
        sys.exit(1)
    except urllib.error.HTTPError as e:
        detail = _format_http_error(
            e, session.get("accessJwt", ""), session.get("refreshJwt", ""))
        sys.stderr.write(f"ERROR: {nsid}: {detail}\n")
        sys.exit(1)
    except urllib.error.URLError as e:
        sys.stderr.write(f"ERROR: network: {e.reason}\n")
        sys.exit(1)
    except http.client.HTTPException as e:
        sys.stderr.write(f"ERROR: {nsid}: incomplete response: {type(e).__name__}: {e}\n")
        sys.exit(1)
