"""Slack Web API request helper. Stdlib-only (#2031, #2032).

Shared by the `slack` watch source (reads: `conversations.history`,
`conversations.replies`, `auth.test`) and `presets/slack/publish.py` (writes:
`chat.postMessage`), the same way `devto/_rest.py` is shared by every op
under `presets/devto/`.

Every credentialed request goes through `presets/_http.py::urlopen()`, never
bare `urllib.request.urlopen` -- see that module's docstring for why: a
same-origin-only redirect policy stands between a Slack `302` and this bot
token leaving to an attacker-controlled host, and `tests/
test_security_redirect.py` fails the build on any bare `urlopen(` under
`presets/`. Every response is read through `read_capped()`, never a bare
`.read()`, for the same reason and the same enforced test
(`tests/test_http_bounds.py`).

Slack signals its OWN failures with `"ok": false` inside an HTTP 200, never
with an HTTP error status -- so `call()` returning a dict is not yet success.
Every caller must check `resp.get("ok")` itself; `SlackTransportError` is
raised only for a *transport* failure (network, redirect refused, oversized
or dripped body, malformed JSON), which is a different question from "did
Slack accept the request".
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

sys.path.insert(0, str(Path(__file__).parent.parent))  # for _http, _secrets (#691)

from _http import (  # noqa: E402
    ERROR_BODY_BYTES,
    DeadlineExceeded,
    RedirectRefused,
    ResponseTooLarge,
    read_capped,
    urlopen,
)
import _secrets  # noqa: E402

BASE = "https://slack.com/api"


class SlackTransportError(Exception):
    """Any failure fetching or parsing a Slack API response.

    One exception type for every transport failure -- a network error, a
    refused off-origin redirect, an oversized or dripped body, an HTTP error
    status, or a response that was not valid JSON. A caller that only wants
    to know "did this call get an answer at all" catches this single class
    rather than four urllib/http exceptions plus a JSON decode error.
    """


def _scrub(s: str, *secrets: str) -> str:
    """Redact the live token first (`str.replace`, exact), then sweep for
    anything shaped like a Slack token this process is not currently holding
    (`_secrets.redact`, pattern-based) -- mirrors `devto/_rest.py::_scrub`,
    plus the shape sweep `_secrets.py` adds for values this call never had a
    handle on, e.g. a second token echoed back in a gateway's own error body.
    """
    for secret in secrets:
        if secret and len(secret) >= 6:
            s = s.replace(secret, "[REDACTED]")
    redacted, _n = _secrets.redact(s)
    return redacted


def call(
    method: str,
    token: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    timeout: int = 15,
) -> dict[str, Any]:
    """One Slack Web API call. POST+JSON when `body` is given, GET+query
    otherwise -- Slack's own convention: `chat.postMessage` and friends take
    a JSON body, `conversations.*` read methods take query parameters.

    Raises `SlackTransportError` on any transport failure. Returns the
    parsed JSON envelope on transport success; callers check `envelope["ok"]`
    themselves (see module docstring).
    """
    url = f"{BASE}/{method}"
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "claude-supertool/slack (+https://github.com/Digital-Process-Tools/claude-supertool)",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    else:
        clean = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
        req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urlopen(req, timeout=timeout) as resp:
            text = read_capped(resp).decode("utf-8")
    except RedirectRefused as e:
        raise SlackTransportError(str(e)) from e
    except (ResponseTooLarge, DeadlineExceeded) as e:
        raise SlackTransportError(_scrub(str(e), token)) from e
    except http.client.HTTPException as e:
        # IncompleteRead subclasses HTTPException, not OSError (#766).
        raise SlackTransportError(f"incomplete response: {type(e).__name__}: {e}") from e
    except urllib.error.HTTPError as e:
        err_body = _scrub(e.read(ERROR_BODY_BYTES).decode("utf-8", errors="replace"), token)
        raise SlackTransportError(f"HTTP {e.code} {e.reason}: {err_body[:300]}") from e
    except urllib.error.URLError as e:
        raise SlackTransportError(f"network: {e.reason}") from e
    except ValueError as e:
        # http.client.InvalidURL subclasses ValueError -- a control character
        # in a caller-supplied channel id or message reaches here as a raw
        # traceback without this catch.
        raise SlackTransportError(f"invalid request: {e}") from e
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        raise SlackTransportError(f"bad JSON response: {_scrub(text, token)[:300]}") from None
    if not isinstance(parsed, dict):
        raise SlackTransportError(f"unexpected payload shape from Slack for {method!r}")
    return parsed
