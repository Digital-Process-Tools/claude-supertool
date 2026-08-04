"""Hashnode GraphQL request helper. Stdlib-only."""
from __future__ import annotations

import http.client
import json
import sys
import urllib.error
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

ENDPOINT = "https://gql.hashnode.com"


def _scrub_token(s: str, token: str) -> str:
    """Redact the auth token from any string before printing it.

    Defense against upstream proxies that echo `Authorization: <token>` in
    error bodies, OS errors that include auth context, or GraphQL servers
    that mention the token in validation messages. Cheap, idempotent, and
    cheaper than a credential leak.
    """
    if not token or not s:
        return s
    return s.replace(token, "[REDACTED]")


def gql(query: str, variables: dict[str, Any], token: str, timeout: int = 30) -> dict[str, Any]:
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(
        ENDPOINT,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": token,
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = read_capped(resp).decode("utf-8")
    except RedirectRefused as e:
        print(f"ERROR: {_scrub_token(str(e), token)}", file=sys.stderr)
        sys.exit(1)
    except (ResponseTooLarge, DeadlineExceeded) as e:
        sys.stderr.write(f"ERROR: {_scrub_token(str(e), token)}\n")
        sys.exit(1)
    except urllib.error.HTTPError as e:
        msg = _format_http_error(e, token)
        sys.stderr.write(f"ERROR: {msg}\n")
        sys.exit(1)
    except urllib.error.URLError as e:
        sys.stderr.write(f"ERROR: network: {_scrub_token(str(e.reason), token)}\n")
        sys.exit(1)
    except http.client.HTTPException as e:
        # IncompleteRead and friends subclass HTTPException, not OSError, so
        # they walk straight past every handler above (#766).
        sys.stderr.write(
            f"ERROR: incomplete response from Hashnode: {type(e).__name__}: "
            f"{_scrub_token(str(e), token)}\n")
        sys.exit(1)
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        sys.stderr.write(f"ERROR: bad JSON from Hashnode\n")
        sys.exit(1)
    if "errors" in data:
        first = data["errors"][0]
        msg = _scrub_token(first.get("message", "unknown GraphQL error"), token)
        code = (first.get("extensions") or {}).get("code", "")
        hint = _hint_for(msg, code)
        sys.stderr.write(f"ERROR: {msg}{(' — ' + hint) if hint else ''}\n")
        sys.exit(1)
    return data.get("data", {})


def gql_safe(query: str, variables: dict[str, Any], token: str, timeout: int = 30) -> dict[str, Any] | None:
    """Like gql() but returns None on a failed lookup instead of exiting. For
    preflight checks that must degrade gracefully — the caller decides whether to
    abort, warn, or proceed when the lookup itself fails.

    The contract, stated once and in full. None is returned for every way the
    *lookup* can fail: a network error, an HTTP error status, a response that
    ends short of its declared length (`IncompleteRead`), a body that does not
    parse, a deadline that runs out, or a GraphQL `errors` array.

    Two failures are not lookups failing, and both exit instead:

    * `RedirectRefused` — an attempt to capture the token (#761). Degrading
      gracefully past it would be the thing that hides it.
    * `ResponseTooLarge` — a body over the cap (#766). It is a statement about
      the endpoint, and returning None makes it indistinguishable from a
      publication that simply has no posts.

    Both are deliberate carve-outs from "returns None on any error", which is
    why they are named here rather than left for a reader to discover."""
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(
        ENDPOINT,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": token,
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = read_capped(resp).decode("utf-8")
        data = json.loads(body)
    except (RedirectRefused, ResponseTooLarge) as e:
        print(f"ERROR: {_scrub_token(str(e), token)}", file=sys.stderr)
        sys.exit(1)
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        json.JSONDecodeError,
        OSError,          # covers DeadlineExceeded, which is a TimeoutError
        http.client.HTTPException,  # IncompleteRead is NOT an OSError (#766)
    ):
        return None
    if "errors" in data:
        return None
    return data.get("data", {})


def _format_http_error(e: urllib.error.HTTPError, token: str = "") -> str:
    # Scrubbed here rather than by the caller (#691): the caller scrubbed the
    # *formatted* string, by which point the body had already been cut to 200
    # chars — a token straddling that boundary survived `replace()` as a
    # fragment, because the fragment is not the string being replaced.
    body = _scrub_token(e.read(ERROR_BODY_BYTES).decode("utf-8", errors="replace"), token)
    if e.code == 401:
        return "401 Unauthorized — check HASHNODE_TOKEN, may have expired"
    if e.code == 403:
        return "403 Forbidden — token lacks required scope or publication access"
    if e.code == 404:
        return "404 Not Found — endpoint moved? check Hashnode API status"
    if e.code == 429:
        return "429 Rate Limited — wait and retry"
    short = body[:200].replace("\n", " ")
    return f"HTTP {e.code} {e.reason}: {short}"


def _hint_for(msg: str, code: str) -> str:
    m = msg.lower()
    if "not authenticated" in m or "unauthorized" in m:
        return "check HASHNODE_TOKEN env var"
    if "publication" in m and "not found" in m:
        return "check HASHNODE_PUBLICATION_ID"
    if "post not found" in m or "post does not exist" in m:
        return "verify slug or post ID"
    if code == "GRAPHQL_VALIDATION_FAILED":
        return "schema mismatch — likely a supertool bug, please report"
    return ""
