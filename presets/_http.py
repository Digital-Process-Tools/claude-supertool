











































































































from __future__ import annotations

import http.client
import ipaddress
import os
import socket
import sys
import time
import urllib.parse
import urllib.request
from typing import Any, Iterable, Sequence

MAX_REDIRECTS = 5








MAX_RESPONSE_BYTES = 10 * 1024 * 1024





ERROR_BODY_BYTES = 64 * 1024




DEADLINE_FACTOR = 4

_CHUNK = 64 * 1024
_DEADLINE_ATTR = "_st_deadline"
_DEFAULT_PORTS = {"http": 80, "https": 443}
_UNPARSEABLE_PORT = -1


class ResponseTooLarge(Exception):








    def __init__(self, url: str, limit: int, declared: int | None = None) -> None:












        url = _scrub_query_secrets(url)
        super().__init__(url, limit, declared)
        self.url = url
        self.limit = limit
        self.declared = declared

    def __str__(self) -> str:
        size = (
            f"declared {self.declared} bytes"
            if self.declared is not None
            else "kept sending past the cap"
        )
        return (
            f"response too large: {self.url!r} {size}, over the {self.limit}-byte cap. "
            f"The body was NOT read and NOT truncated — a truncated body would have "
            f"been reported to you as malformed data from the endpoint."
        )


class DeadlineExceeded(TimeoutError):








    def __init__(self, url: str, seconds: float) -> None:



        url = _scrub_query_secrets(url)
        super().__init__(url, seconds)
        self.url = url
        self.seconds = seconds

    def __str__(self) -> str:
        return (
            f"exceeded the {self.seconds:g}s deadline reading {self.url!r}. urllib's "
            f"timeout bounds each socket read, not the call: a server sending one "
            f"byte at a time resets it forever."
        )


class DestinationRefused(Exception):










    def __init__(self, url: str, reason: str) -> None:






        url = _scrub_query_secrets(url)
        super().__init__(url, reason)
        self.url = url
        self.reason = reason

    def __str__(self) -> str:


        return (
            f"refused to fetch {self.url!r}: {self.reason}. Nothing was requested "
            f"from that destination and nothing was written to disk."
        )


class RedirectRefused(Exception):









    def __init__(self, from_url: str, to_url: str, code: int, reason: str) -> None:












        from_url = _scrub_query_secrets(from_url)
        to_url = _scrub_query_secrets(to_url)
        super().__init__(from_url, to_url, code, reason)
        self.from_url = from_url
        self.to_url = to_url
        self.code = code
        self.reason = reason

    def __str__(self) -> str:








        return (
            f"refused off-origin redirect: {self.from_url!r} answered HTTP {self.code} "
            f"pointing at {self.to_url!r} ({self.reason}). The redirect was NOT followed "
            f"and no credentials were sent to that destination."
        )


def origin(url: str) -> tuple[str, str, int]:





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






def check_address(addr: str) -> str | None:













    try:
        ip: Any = ipaddress.ip_address(addr)
    except ValueError:
        return f"{addr!r} is not an IP address"
    for attr in ("ipv4_mapped", "sixtofour", "teredo"):
        inner = getattr(ip, attr, None)
        if inner is not None:

            ip = inner[1] if isinstance(inner, tuple) else inner
    if ip.is_multicast:
        return f"{ip} is a multicast address, not a public unicast one"
    if not ip.is_global:
        return (
            f"{ip} is not a public address (loopback, link-local, private, "
            f"shared or otherwise reserved)"
        )
    return None


def host_allowed(host: str, allowed_hosts: Iterable[str]) -> bool:







    host = (host or "").lower().rstrip(".")
    if not host:
        return False
    for entry in allowed_hosts:
        entry = entry.lower().strip().rstrip(".")
        if not entry:
            continue
        if entry.startswith("."):
            if host.endswith(entry) and len(host) > len(entry):
                return True
        elif host == entry:
            return True
    return False


def check_fetch_target(
    url: str,
    allowed_hosts: Iterable[str],
    *,
    allow_private: bool = False,
    allow_schemes: Sequence[str] = ("http", "https"),
) -> str | None:









    parts = urllib.parse.urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in allow_schemes:
        return f"scheme {scheme or '(none)'} is not one of {'/'.join(allow_schemes)}"
    try:
        host = parts.hostname
    except ValueError:
        return "the URL has an unparseable host"
    if not host:
        return "the URL has no host"
    if not host_allowed(host, allowed_hosts):
        return (
            f"host {host!r} is not on the fetch allowlist "
            f"({', '.join(sorted(allowed_hosts)) or 'empty'})"
        )
    if not allow_private:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            reason = check_address(host)
            if reason is not None:
                return reason
    return None


class DestinationRedirectHandler(urllib.request.HTTPRedirectHandler):











    max_redirections = MAX_REDIRECTS

    def __init__(self, allowed_hosts: Sequence[str], allow_private: bool) -> None:
        self.allowed_hosts = tuple(allowed_hosts)
        self.allow_private = allow_private

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        reason = check_fetch_target(
            newurl, self.allowed_hosts, allow_private=self.allow_private
        )
        if reason is not None:
            raise DestinationRefused(
                newurl, f"HTTP {code} from {req.full_url!r} redirected here, and {reason}"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_opener(
    allowed_hosts: Sequence[str], allow_private: bool
) -> urllib.request.OpenerDirector:





    return urllib.request.build_opener(
        DestinationRedirectHandler(allowed_hosts, allow_private)
    )


def _check_resolved(host: str, allow_private: bool) -> None:







    if allow_private:
        return
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return
    for info in infos:
        reason = check_address(info[4][0])
        if reason is not None:
            raise DestinationRefused(host, f"it resolves to {reason}")


def download(
    url: str,
    dest: str,
    *,
    allowed_hosts: Sequence[str],
    limit: int | None = None,
    timeout: int = 20,
    deadline: float | None = None,
    allow_private: bool = False,
    content_types: Sequence[str] | None = None,
) -> int:









































    reason = check_fetch_target(url, allowed_hosts, allow_private=allow_private)
    if reason is not None:
        raise DestinationRefused(url, reason)
    host = urllib.parse.urlsplit(url).hostname or ""
    _check_resolved(host, allow_private)

    opener = download_opener(allowed_hosts, allow_private)
    with urlopen(url, timeout=timeout, deadline=deadline, opener=opener) as resp:
        if content_types:
            got = (getattr(resp, "headers", None) or {}).get("Content-Type", "") or ""
            base = got.split(";")[0].strip().lower()
            if not any(base.startswith(p) for p in content_types):
                resp.close()
                raise DestinationRefused(
                    url,
                    f"it answered Content-Type {base or '(none)'!r}, not one of "
                    f"{'/'.join(content_types)}*. The body was not read and not saved",
                )
        data = read_capped(resp, limit=limit)









    parent = os.path.dirname(dest)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = dest + ".part"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, dest)
    return len(data)


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):








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










_OPEN = _OPENER.open


def _declared_length(resp: Any) -> int | None:

    headers = getattr(resp, "headers", None)
    if headers is None:
        return None
    if (headers.get("Transfer-Encoding") or "").lower() == "chunked":
        return None
    raw = headers.get("Content-Length")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _check_deadline(deadline: tuple[float, float] | None, url: str) -> None:

    if deadline is not None and time.monotonic() > deadline[0]:
        raise DeadlineExceeded(url, deadline[1])


def read_capped(
    resp: Any,
    limit: int | None = None,
    deadline: tuple[float, float] | None = None,
) -> bytes:

















    if limit is None:
        limit = MAX_RESPONSE_BYTES
    if deadline is None:
        deadline = getattr(resp, _DEADLINE_ATTR, None)
    url = getattr(resp, "url", "") or ""

    declared = _declared_length(resp)
    if declared is not None and declared > limit:
        resp.close()
        raise ResponseTooLarge(url, limit, declared)

    read1 = getattr(resp, "read1", None) or resp.read
    chunks: list[bytes] = []
    total = 0
    while True:
        _check_deadline(deadline, url)
        chunk = read1(min(_CHUNK, limit + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            resp.close()
            raise ResponseTooLarge(url, limit, declared)
        chunks.append(chunk)
    _check_deadline(deadline, url)

    if declared is not None and total < declared:



        raise http.client.IncompleteRead(b"", declared - total)
    return b"".join(chunks)





















_SENSITIVE_QUERY_PARAMS = frozenset({
    "key", "api_key", "apikey", "access_token", "token", "secret",
    "client_secret", "password", "auth", "id_token", "refresh_token",
    "session_token", "csrf_token",
})


def _scrub_query_secrets(url: str) -> str:



















    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    if not parts.query:
        return url
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    scrubbed = [
        (k, "[REDACTED]" if k.lower().replace("-", "_") in _SENSITIVE_QUERY_PARAMS else v)
        for k, v in pairs
    ]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(scrubbed)))


def _origin_and_path(url: str) -> str:









































    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return url.split("?", 1)[0].split("#", 1)[0]
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def urlopen(
    req: urllib.request.Request | str,
    timeout: int = 30,
    deadline: float | None = None,
    opener: urllib.request.OpenerDirector | None = None,
) -> Any:




































    requested = req.full_url if isinstance(req, urllib.request.Request) else req
    if deadline is None:
        deadline = timeout * DEADLINE_FACTOR
    expires = (time.monotonic() + deadline, float(deadline)) if deadline > 0 else None







    do_open = _OPEN if opener is None else opener.open
    resp = do_open(req, timeout=timeout)
    final = getattr(resp, "url", None)
    if final and final != requested:
















        disclosed_requested = _origin_and_path(requested)
        disclosed_final = _origin_and_path(final)









        if disclosed_requested == disclosed_final:
            detail = (
                "only the query string differed between them, and it is "
                "omitted from this disclosure -- #2533"
            )
        else:
            detail = "query strings omitted from this disclosure -- #2533"
        print(
            f"NOTE: the request was redirected before it was answered: "
            f"{disclosed_requested!r} -> {disclosed_final!r}. "
            f"The response came from the second URL. "
            f"The hop stayed on the same origin, so it was followed. "
            f"({detail})",
            file=sys.stderr,
        )
    if expires is not None:
        try:
            setattr(resp, _DEADLINE_ATTR, expires)
        except (AttributeError, TypeError):
            pass
        if time.monotonic() > expires[0]:
            resp.close()
            raise DeadlineExceeded(final or requested, expires[1])
    return resp
