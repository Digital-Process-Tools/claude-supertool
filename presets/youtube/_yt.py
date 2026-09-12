"""YouTube Data API v3 GET helper built on the shared `_http` opener.

Stdlib-only, read-only surface: search.list, videos.list, commentThreads.list,
channels.list, playlistItems.list. There is no OAuth2 flow here and no write
verb -- the raw REST endpoint is called directly with an API key query
parameter, following presets/_http.py's redirect and size/deadline guards the
same way presets/bluesky/_atproto.py does, rather than a vendored Google API
client library (this repo is stdlib-only per CLAUDE.md, and #227 itself only
ever sketches raw endpoint mappings).
"""
from __future__ import annotations

import http.client
import json
import sys
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

API_BASE = "https://www.googleapis.com/youtube/v3"


class YouTubeAPIError(Exception):
    """Any failure resolving a YouTube Data API v3 call.

    Carries `endpoint` and `message` separately so a caller that wants a soft
    degrade (read.py's comment fetch, when comments are disabled on a video)
    can catch this alone and continue without comments, rather than the whole
    read failing over one optional call. The API key is never present in
    `message` -- see `_scrub` below.
    """

    def __init__(self, endpoint: str, message: str) -> None:
        super().__init__(f"{endpoint}: {message}")
        self.endpoint = endpoint
        self.message = message


def _scrub(s: str, api_key: str) -> str:
    """Redact the API key from a string before it reaches an exception message.

    The key travels as a `key=` query parameter, so it is part of the request
    URL itself -- and `URLError`/`HTTPError` messages, and any echo an error
    body performs, can carry the full URL back. Mirrors
    presets/bluesky/_atproto.py::_scrub for the same reason: a redacted
    request is cheaper than a leaked credential, and idempotent when the key
    never appears at all.
    """
    if api_key and len(api_key) >= 6:
        return s.replace(api_key, "[REDACTED]")
    return s


def _format_http_error(e: urllib.error.HTTPError, api_key: str) -> str:
    body = _scrub(e.read(ERROR_BODY_BYTES).decode("utf-8", errors="replace"), api_key)
    if e.code == 400:
        return f"400 Bad Request: {body[:300]}"
    if e.code == 403:
        return f"403 Forbidden (quota exceeded, key restricted, or the API not enabled?): {body[:300]}"
    if e.code == 404:
        return f"404 Not Found: {body[:300]}"
    return f"HTTP {e.code} {e.reason}: {body[:300]}"


def get(endpoint: str, api_key: str, params: dict[str, Any], timeout: int = 15) -> dict[str, Any]:
    """GET https://www.googleapis.com/youtube/v3/{endpoint}?...&key=API_KEY.

    Raises `YouTubeAPIError(endpoint, message)` for every failure mode -- a
    non-2xx response, a network error, a redirect refused off-origin, a body
    over the size cap, a deadline that ran out, or a body that does not parse
    as JSON. Callers decide per call site whether that is fatal (exit 1) or a
    degrade (read.py's comments).
    """
    query = dict(params)
    query["key"] = api_key
    url = f"{API_BASE}/{endpoint}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = read_capped(resp).decode("utf-8")
    except RedirectRefused as e:
        raise YouTubeAPIError(endpoint, _scrub(str(e), api_key)) from e
    except (ResponseTooLarge, DeadlineExceeded) as e:
        raise YouTubeAPIError(endpoint, _scrub(str(e), api_key)) from e
    except urllib.error.HTTPError as e:
        raise YouTubeAPIError(endpoint, _format_http_error(e, api_key)) from e
    except urllib.error.URLError as e:
        raise YouTubeAPIError(endpoint, f"network error: {e.reason}") from e
    except http.client.HTTPException as e:
        raise YouTubeAPIError(
            endpoint, f"incomplete response: {type(e).__name__}: {e}") from e
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise YouTubeAPIError(endpoint, f"could not parse response as JSON: {e}") from e
