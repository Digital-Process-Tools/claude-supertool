"""Dev.to REST request helper. Stdlib-only."""
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

BASE = "https://dev.to/api"


def _scrub(s: str, *secrets: str) -> str:
    """Redact credential material from a string before printing it (#691).

    Mirrors `presets/hashnode/_graphql.py::_scrub_token`, which this module
    lacked: the API key travels in a request header, and a gateway that echoes
    request headers back in a 4xx/5xx body put it straight on stderr.

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
    if e.code == 401:
        return "401 Unauthorized — check DEVTO_API_KEY, may have expired"
    if e.code == 403:
        return "403 Forbidden — Dev.to blocks default UA or token scope insufficient"
    if e.code == 404:
        return "404 Not Found — article ID/slug invalid"
    if e.code == 422:
        return f"422 Unprocessable — bad request shape: {body[:200]}"
    if e.code == 429:
        return "429 Rate Limited — Dev.to allows 1 post per 5 min on new accounts; wait and retry"
    short = body[:200].replace("\n", " ")
    return f"HTTP {e.code} {e.reason}: {short}"


def request(
    method: str,
    path: str,
    api_key: str,
    body: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
    timeout: int = 30,
) -> Any:
    url = f"{BASE}{path}"
    if query:
        clean = {k: v for k, v in query.items() if v is not None and v != ""}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
    data = None
    headers = {
        "api-key": api_key,
        "Accept": "application/vnd.forem.api-v1+json",
        "User-Agent": "claude-supertool/devto (+https://github.com/Digital-Process-Tools/claude-supertool)",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as resp:
            text = read_capped(resp).decode("utf-8")
    except RedirectRefused as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except (ResponseTooLarge, DeadlineExceeded) as e:
        sys.stderr.write(f"ERROR: {_scrub(str(e), api_key)}\n")
        sys.exit(1)
    except http.client.HTTPException as e:
        # IncompleteRead subclasses HTTPException, not OSError (#766).
        sys.stderr.write(f"ERROR: incomplete response: {type(e).__name__}: {e}\n")
        sys.exit(1)
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"ERROR: {_format_http_error(e, api_key)}\n")
        sys.exit(1)
    except urllib.error.URLError as e:
        sys.stderr.write(f"ERROR: network: {e.reason}\n")
        sys.exit(1)
    except ValueError as e:
        # http.client.InvalidURL subclasses ValueError. Triggered when the
        # URL contains control chars (e.g. spaces from shell-meta input that
        # slipped past resolve_article_id). Without this catch the raw
        # traceback leaks; with it the user sees a clean rejection.
        sys.stderr.write(f"ERROR: invalid URL: {e}\n")
        sys.exit(1)
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        sys.stderr.write(
            f"ERROR: bad JSON response: {_scrub(text, api_key)[:500]}\n")
        sys.exit(1)
