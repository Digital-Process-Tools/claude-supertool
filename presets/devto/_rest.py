"""Dev.to REST request helper. Stdlib-only."""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

BASE = "https://dev.to/api"


def _format_http_error(e: urllib.error.HTTPError) -> str:
    body = e.read().decode("utf-8", errors="replace")
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
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"ERROR: {_format_http_error(e)}\n")
        sys.exit(1)
    except urllib.error.URLError as e:
        sys.stderr.write(f"ERROR: network: {e.reason}\n")
        sys.exit(1)
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        sys.stderr.write(f"ERROR: bad JSON response: {text[:500]}\n")
        sys.exit(1)
