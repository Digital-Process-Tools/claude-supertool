"""Refuse outbound network from a test, and name the host it tried to reach.

A test that reaches a third-party host makes its own red leg a statement about
somebody else's DNS and redirect policy. `test_hashnode.py::
test_comment_main_aborts_on_dupe` did exactly that (#1312): it stubbed the op's
`gql` and left `gql_safe` live, so every leg of every PR opened a real HTTPS
connection to `gql.hashnode.com` -- and the test passed on whatever came back,
because a failed lookup returns None and None is also an ABORT.

That is this repo's own defect class: an absence produced by the environment,
read as a product verdict. The guard here converts it into the third state.
An unstubbed transport now fails **at the socket**, naming the destination and
what to stub, instead of quietly succeeding against the internet.

Loopback and AF_UNIX are allowed on purpose: `test_http_bounds.py` and the
`claude-channel` suites bind real servers on `127.0.0.1` / a unix socket, and
those are hermetic -- the process under test is the one that answered.
"""
from __future__ import annotations

import socket
from typing import Any

import pytest

#: Addresses that are the test process talking to itself.
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost", "", "0.0.0.0"})

_AF_UNIX = getattr(socket, "AF_UNIX", None)


class OutboundBlocked(BaseException):
    """A test tried to open a connection to a host it does not control.

    `BaseException`, not `AssertionError`, and the reason is the defect this
    file exists to prevent. Presets catch broadly and by design -- `_http.gql_safe`
    swallows every OSError to return None, and `hashnode/comment.py::main` wraps
    its `_me.get_username` call in a bare `except Exception` that renders the
    exception into an `ABORT -- pre-flight failed (cannot identify user: ...)`
    line. An `Exception` here would be absorbed by exactly those arms, printed
    as a plausible ABORT, and a test asserting `"ABORT" in err` would pass: the
    guard's own signal read as a product verdict, which is #1312 one layer up.
    Nothing in this repo catches `BaseException`, so the refusal reaches pytest.
    """


def _refuse(what: str, target: Any) -> None:
    raise OutboundBlocked(
        f"{what} to {target!r} was blocked. Adapter tests stub their transport: "
        "patch the op's `gql` / `gql_safe` / `_http._OPEN`, not just one of them. "
        "A live call makes this leg a statement about somebody else's DNS and "
        "redirect policy, and it passes or fails for reasons the diff cannot "
        "cause (#1312)."
    )


def block_outbound(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block non-loopback sockets for the duration of one test.

    Both `getaddrinfo` and `connect` are covered. Either alone leaves a hole:
    a name lookup is already egress and already blocks the runner, and an
    address literal reaches `connect` without ever resolving anything.
    """
    orig_connect = socket.socket.connect
    orig_getaddrinfo = socket.getaddrinfo

    def _is_local(host: Any) -> bool:
        # `bytes` on purpose: urllib passes `str`, but a caller further down the
        # stack may not, and `str(b"127.0.0.1")` is `"b'127.0.0.1'"` -- which
        # would block loopback and read exactly like a real egress finding.
        if isinstance(host, (bytes, bytearray)):
            host = host.decode("ascii", errors="replace")
        return str(host) in LOOPBACK

    def _connect(self: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else address
        if (_AF_UNIX is not None and self.family == _AF_UNIX) or _is_local(host):
            return orig_connect(self, address)
        _refuse("an outbound connect", address)

    def _getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        if host is not None and not _is_local(host):
            _refuse("a DNS lookup", (host, port))
        return orig_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", _connect)
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo)
