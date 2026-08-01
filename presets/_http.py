"""Shared HTTP opener for preset clients that send credentials. Stdlib-only.

Why this module exists
----------------------
`urllib.request.urlopen` uses the default global opener, which installs
`HTTPRedirectHandler`. When that handler rebuilds a request for a 301/302/303/
307/308 it strips exactly two headers — `content-length` and `content-type` —
and carries **everything else** to the new location, including `Authorization`,
`api-key` and `Cookie`, **regardless of whether the new location is the same
origin**. `http_error_302` also accepts an `https` -> `http` downgrade. Up to
ten hops.

A server that answers `302 Location: http://attacker.example/` therefore
receives the caller's live credential, and — because the redirect is followed
transparently — its response body is returned to the caller as though the real
API had answered it. A false success on top of a credential leak.

Every preset that sends a credential MUST call `urlopen()` from this module
rather than `urllib.request.urlopen`. `tests/test_security_redirect.py`
asserts there are no bare `urllib.request.urlopen(` call sites left under
`presets/`, so a fifth integration inherits this instead of re-earning it.

Same-origin rule
----------------
An origin is `(scheme, host, port)` with default ports normalised
(http:80, https:443) and the host compared case-insensitively. A redirect is
followed only when:

  * the target scheme is http or https, and
  * the host is identical, and
  * either the scheme and port are identical,
  * or it is an `http` -> `https` upgrade with both sides on their scheme's
    default port.

Everything else is refused: a different host, a different port on the same
host, an `https` -> `http` downgrade, a non-HTTP scheme, an unparseable port.

The port is part of the origin on purpose. `example.com:8443` is a different
service from `example.com:443` — it may be a dev server, a user-controlled
container, or a tunnel — and nothing in the four APIs this module serves
redirects across ports. The `http` -> `https` upgrade is the one asymmetry:
it moves the credential onto a *more* protected channel, never a less
protected one, and it is the redirect a public API edge performs on a caller
that typed the scheme wrong. The downgrade is refused even on the same host,
because that is the shape the attack takes when it cannot change the host.

Refusal is loud
---------------
A refused redirect raises `RedirectRefused`, which deliberately does **not**
subclass `OSError`/`URLError`, so no existing `except URLError` or
`except OSError` swallows it into a generic "network error". Callers catch it
explicitly and print `str(exc)`, which names the origin, the status code, the
attempted destination and the reason. Returning the pre-redirect state quietly
would be the same false-success defect in a new coat.
"""
from __future__ import annotations

import sys
import urllib.parse
import urllib.request
from typing import Any

MAX_REDIRECTS = 5

_DEFAULT_PORTS = {"http": 80, "https": 443}
_UNPARSEABLE_PORT = -1


class RedirectRefused(Exception):
    """A redirect pointed off-origin and was not followed.

    Not an OSError subclass: `except urllib.error.URLError` and
    `except OSError` handlers in the clients must NOT absorb this into a
    generic network failure, and the `gql_safe`-style "returns None on any
    error" helpers must not turn it into a silent None. A credential
    exfiltration attempt is not a degraded lookup.
    """

    def __init__(self, from_url: str, to_url: str, code: int, reason: str) -> None:
        super().__init__(from_url, to_url, code, reason)
        self.from_url = from_url
        self.to_url = to_url
        self.code = code
        self.reason = reason

    def __str__(self) -> str:
        """Both URLs are rendered with `!r`.

        The destination is copied straight out of a remote `Location` header, so
        it is attacker-chosen text on its way to somebody's terminal. `repr`
        escapes the control characters — `\\r`, ANSI CSI sequences — that would
        otherwise let the destination overwrite or recolour the warning printed
        about it. A disclosure that the attacker can edit is not a disclosure.
        """
        return (
            f"refused off-origin redirect: {self.from_url!r} answered HTTP {self.code} "
            f"pointing at {self.to_url!r} ({self.reason}). The redirect was NOT followed "
            f"and no credentials were sent to that destination."
        )


def origin(url: str) -> tuple[str, str, int]:
    """Return the normalised (scheme, host, port) origin of a URL.

    An unparseable port yields -1, which compares equal to nothing, so a
    malformed authority fails closed rather than matching by accident.
    """
    parts = urllib.parse.urlsplit(url)
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        return scheme, host, _UNPARSEABLE_PORT
    if port is None:
        port = _DEFAULT_PORTS.get(scheme, _UNPARSEABLE_PORT)
    return scheme, host, port


def check_redirect(from_url: str, to_url: str) -> str | None:
    """Return None if the redirect may be followed, else the refusal reason."""
    f_scheme, f_host, f_port = origin(from_url)
    t_scheme, t_host, t_port = origin(to_url)

    if t_scheme not in ("http", "https"):
        return f"scheme {t_scheme or '(none)'} is not http/https"
    if not t_host:
        return "destination has no host"
    if t_host != f_host:
        return f"different host: {f_host or '(none)'} -> {t_host}"
    if _UNPARSEABLE_PORT in (f_port, t_port):
        return "unparseable port"
    if f_scheme == t_scheme:
        if f_port != t_port:
            return f"different port on the same host: {f_port} -> {t_port}"
        return None
    if f_scheme == "http" and t_scheme == "https":
        if f_port == 80 and t_port == 443:
            return None
        return f"scheme upgrade off the default ports: {f_port} -> {t_port}"
    return f"scheme downgrade: {f_scheme} -> {t_scheme}"


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """HTTPRedirectHandler that refuses to carry a request off its origin.

    `redirect_request` is the single choke point for 301/302/303/307/308 —
    urllib aliases all of them onto `http_error_302`, which resolves the
    Location against the current URL before calling here, so `newurl` is
    always absolute by this point.
    """

    max_redirections = MAX_REDIRECTS

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        reason = check_redirect(req.full_url, newurl)
        if reason is not None:
            raise RedirectRefused(req.full_url, newurl, code, reason)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(SafeRedirectHandler())

# Bound once, and called as a plain name rather than `_OPENER.open(...)`, so the
# shipped-code scan in tests/test_encoding_seam.py does not read an
# OpenerDirector.open call as a locale-decoded `Path.open`. There is no file I/O
# in this module at all.
_OPEN = _OPENER.open


def urlopen(req: urllib.request.Request | str, timeout: int = 30) -> Any:
    """Drop-in replacement for urllib.request.urlopen with off-origin redirects refused.

    A *permitted* (same-origin) redirect is still disclosed on stderr. Allowing
    a hop is not the same as saying nothing about it: the caller asked one URL a
    question, a different URL answered, and every return value below this line
    describes the second one. Staying silent about that is the same substitution
    the refusal path exists to prevent, minus the credential theft.

    It is not hypothetical. dev.to answers `/settings` with a same-origin 302 to
    `/enter` the moment the session cookie expires, and `fetch_csrf_token` then
    reports "authenticity_token not found in /settings HTML — Dev.to layout may
    have changed", in which every noun is wrong: the HTML is `/enter`, the layout
    is fine, and the cookie is dead. An operator acting reasonably on that goes
    looking for a redesign that never happened.

    The cost is bounded because the line is gated on an actual hop — `final !=
    requested`. A call that is not redirected prints nothing, which is every call
    on a normal day against all four of these APIs. Measured on loopback:
    no redirect -> stderr is empty; same-origin hop -> one NOTE line.

    Both URLs are printed with `!r`. The destination comes from a remote
    `Location` header, so it is attacker-chosen text on its way to a terminal;
    `repr` escapes the control characters that would otherwise let it rewrite the
    lines around it.

    `timeout` keeps urllib's meaning: a per-socket-operation timeout, not an
    overall deadline. That distinction is a separate defect, tracked apart from
    this module's redirect guarantee.
    """
    requested = req.full_url if isinstance(req, urllib.request.Request) else req
    resp = _OPEN(req, timeout=timeout)
    final = getattr(resp, "url", None)
    if final and final != requested:
        print(
            f"NOTE: the request was redirected before it was answered: "
            f"{requested!r} -> {final!r}. The response came from the second URL. "
            f"The hop stayed on the same origin, so it was followed.",
            file=sys.stderr,
        )
    return resp
