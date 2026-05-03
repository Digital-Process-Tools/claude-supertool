"""Hashnode GraphQL request helper. Stdlib-only."""
import json
import sys
import urllib.error
import urllib.request
from typing import Any

ENDPOINT = "https://gql.hashnode.com"


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
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        msg = _format_http_error(e)
        sys.stderr.write(f"ERROR: {msg}\n")
        sys.exit(1)
    except urllib.error.URLError as e:
        sys.stderr.write(f"ERROR: network: {e.reason}\n")
        sys.exit(1)
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        sys.stderr.write(f"ERROR: bad JSON from Hashnode\n")
        sys.exit(1)
    if "errors" in data:
        first = data["errors"][0]
        msg = first.get("message", "unknown GraphQL error")
        code = (first.get("extensions") or {}).get("code", "")
        hint = _hint_for(msg, code)
        sys.stderr.write(f"ERROR: {msg}{(' — ' + hint) if hint else ''}\n")
        sys.exit(1)
    return data.get("data", {})


def gql_safe(query: str, variables: dict[str, Any], token: str, timeout: int = 30) -> dict[str, Any] | None:
    """Like gql() but returns None on any error instead of exiting. For preflight
    checks that must degrade gracefully — caller decides whether to abort, warn,
    or proceed when the lookup itself fails."""
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
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
        data = json.loads(body)
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError, OSError):
        return None
    if "errors" in data:
        return None
    return data.get("data", {})


def _format_http_error(e: urllib.error.HTTPError) -> str:
    body = e.read().decode("utf-8", errors="replace")
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
