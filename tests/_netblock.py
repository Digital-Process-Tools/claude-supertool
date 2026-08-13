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

**Its coverage is a register, not a sentence (#1584).** Until then this guard
patched `connect` and `getaddrinfo`, and both `conftest.py` and
`docs/contributing.md` stated its limit as exactly one thing -- "it binds
`socket` in the pytest process only". That reads as exhaustive and was not:
`connect_ex`, `gethostbyname`, `gethostbyname_ex`, `gethostbyaddr`,
`getnameinfo` and a UDP `sendto` all walked out of an armed context, and
`gethostbyname` came back with a live-resolved address. Nothing in the suite
took them, which is why it was low and not a leak.

The lesson is not "patch six more names". A longer list is still a list, and
the next reader trusts it exactly as much as the one-line version. So `ROUTES`
and `SOCKET_ROUTES` below classify **every callable the `socket` module and the
`socket.socket` type expose**, `tests/test_netblock_egress_register_1584.py`
fails when one of them arrives unclassified, and a route that cannot be covered
is `OPEN` with its reason next to it rather than absent. That is this repo's
third state applied to the guard's own boundary: `blocked`, `local`, and *I
cannot see this one, and here is why*.
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


# ---------------------------------------------------------------------------
# The register (#1584). What this guard covers, derived from the interpreter.
# ---------------------------------------------------------------------------

#: Refused here unless the destination is loopback or AF_UNIX.
PATCHED = "patched"
#: Reaches a `PATCHED` route to do its work, so it is refused there. Recorded
#: rather than omitted: "covered because something else covers it" is a claim,
#: and an implementation change can quietly stop making it true.
VIA = "via a patched route"
#: Inbound, or local to this machine. There is no remote destination to refuse.
LOCAL = "local or inbound"
#: Byte-order and address-format arithmetic. Touches nothing.
INERT = "no I/O"
#: Reaches the network and this guard cannot see it. The third state, and the
#: reason is in `OPEN_ROUTES` -- an entry without one is a shorter list, not a
#: boundary.
OPEN = "open"

#: Every callable `socket` exposes. A name the interpreter grows and nobody has
#: classified fails `test_netblock_egress_register_1584.py` rather than sitting
#: inside a guard whose prose still says "connect and getaddrinfo".
ROUTES = {
    "getaddrinfo": PATCHED,
    "getnameinfo": PATCHED,
    "gethostbyname": PATCHED,
    "gethostbyname_ex": PATCHED,
    "gethostbyaddr": PATCHED,
    "create_connection": VIA,
    "getfqdn": VIA,
    "create_server": LOCAL,
    "socketpair": LOCAL,
    "close": LOCAL,
    "gethostname": LOCAL,
    "sethostname": LOCAL,
    "getprotobyname": LOCAL,
    "getservbyname": LOCAL,
    "getservbyport": LOCAL,
    "has_dualstack_ipv6": LOCAL,
    "if_indextoname": LOCAL,
    "if_nameindex": LOCAL,
    "if_nametoindex": LOCAL,
    "recv_fds": LOCAL,
    "send_fds": LOCAL,
    "getdefaulttimeout": INERT,
    "setdefaulttimeout": INERT,
    "htonl": INERT,
    "htons": INERT,
    "ntohl": INERT,
    "ntohs": INERT,
    "inet_aton": INERT,
    "inet_ntoa": INERT,
    "inet_ntop": INERT,
    "inet_pton": INERT,
    "CMSG_LEN": INERT,
    "CMSG_SPACE": INERT,
    "dup": OPEN,
    "fromfd": OPEN,
    "fromshare": OPEN,
}

#: Every method `socket.socket` exposes, same rule.
SOCKET_ROUTES = {
    "connect": PATCHED,
    "connect_ex": PATCHED,
    "sendto": PATCHED,
    "sendmsg": PATCHED,
    "send": VIA,
    "sendall": VIA,
    "sendfile": VIA,
    "accept": LOCAL,
    "bind": LOCAL,
    "listen": LOCAL,
    "close": LOCAL,
    "detach": LOCAL,
    "dup": LOCAL,
    "fileno": LOCAL,
    "makefile": LOCAL,
    "shutdown": LOCAL,
    "recv": LOCAL,
    "recv_into": LOCAL,
    "recvfrom": LOCAL,
    "recvfrom_into": LOCAL,
    "recvmsg": LOCAL,
    "recvmsg_into": LOCAL,
    "sendmsg_afalg": LOCAL,
    "family": INERT,
    "proto": INERT,
    "type": INERT,
    "timeout": INERT,
    "gettimeout": INERT,
    "settimeout": INERT,
    "getblocking": INERT,
    "setblocking": INERT,
    "getsockopt": INERT,
    "setsockopt": INERT,
    "get_inheritable": INERT,
    "set_inheritable": INERT,
    "getpeername": INERT,
    "getsockname": INERT,
    "share": OPEN,
}

#: Why each `OPEN` route is open. Every one of them is the same shape: a
#: descriptor whose `connect` happened somewhere this guard was not watching,
#: so wrapping it in a `socket` object afterwards inherits nothing.
OPEN_ROUTES = {
    "dup": "duplicates a descriptor that may already be connected -- the "
           "connect this guard would have refused happened before the wrap.",
    "fromfd": "builds a socket around an arbitrary descriptor, including one "
              "a child process or a C extension connected.",
    "fromshare": "Windows: rebuilds a socket another process shared, so the "
                 "connect happened in a process this guard never patched.",
    "share": "Windows: hands this socket to another process, which is not "
             "running an armed pytest and is not bound by anything here.",
}

#: Names that legitimately do not exist on every platform or Python. A name
#: missing here is a route that cannot be taken, which is the safe direction;
#: what the register must never carry is a name no Python has.
PLATFORM_OPTIONAL = frozenset({
    "CMSG_LEN", "CMSG_SPACE", "sethostname", "fromshare", "share",
    "if_indextoname", "if_nameindex", "if_nametoindex",
    "recv_fds", "send_fds", "sendmsg", "sendmsg_afalg",
    "recvmsg", "recvmsg_into", "sendfile",
})

_MODULE_PATCHES = tuple(
    n for n, c in ROUTES.items() if c == PATCHED and hasattr(socket, n))
_METHOD_PATCHES = tuple(
    n for n, c in SOCKET_ROUTES.items()
    if c == PATCHED and hasattr(socket.socket, n))

#: What `block_outbound` actually replaces on this interpreter. Derived, so the
#: register cannot claim a route the code does not patch: on Windows there is
#: no `socket.socket.sendmsg` at all, and a hand-maintained list would have
#: read as coverage there.
PATCHES = _MODULE_PATCHES + _METHOD_PATCHES

#: What no in-process patch can reach, stated as the several things it is
#: rather than the one thing `conftest.py` used to call it (#1584).
BEYOND_THE_PROCESS = (
    "A child process. A test that shells out to `supertool.py` or a preset "
    "gets an unpatched interpreter, and nothing there is blocked.",
    "A C extension, or any library calling libc's `connect` directly. The "
    "`socket` module is a Python-level name and rebinding it binds nobody "
    "who does not go through it.",
    "A descriptor connected before this guard was armed, or received over "
    "AF_UNIX with SCM_RIGHTS. The connect happened where the guard was not; "
    "`fromfd`, `dup`, `share` and `fromshare` are the in-module doors to it.",
    "Reads and writes on such a descriptor through `os.read` / `os.write` "
    "rather than a socket object, which reach no method this file replaces.",
)


def _is_local(host: Any) -> bool:
    """True for an address that is the test process talking to itself.

    `bytes` on purpose: urllib passes `str`, but a caller further down the
    stack may not, and `str(b"127.0.0.1")` is `"b'127.0.0.1'"` -- which would
    block loopback and read exactly like a real egress finding.
    """
    if isinstance(host, (bytes, bytearray)):
        host = host.decode("ascii", errors="replace")
    return str(host) in LOOPBACK


def _address_is_local(sock: "socket.socket", address: Any) -> bool:
    """AF_UNIX has no destination to protect; otherwise read the host out."""
    if _AF_UNIX is not None and sock.family == _AF_UNIX:
        return True
    host = address[0] if isinstance(address, tuple) else address
    return _is_local(host)


def block_outbound(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block non-loopback egress for the duration of one test.

    The routes patched here are exactly the names `ROUTES` and `SOCKET_ROUTES`
    classify `PATCHED`, and a test asserts that correspondence rather than
    trusting it -- a guard whose register drifts from its patches documents
    coverage it does not have, which is the defect this file exists for.

    Why each is needed, since "connect and getaddrinfo" once read as complete:
    a name lookup is already egress and already blocks the runner; an address
    literal reaches `connect` without resolving anything; `connect_ex` returns
    an errno instead of raising, so it never touched the patched `connect`; a
    UDP `sendto` carries its destination per-datagram and never connects at
    all; and the four `gethost*`/`getnameinfo` resolvers reach the resolver
    without going through `getaddrinfo`.
    """
    orig = {name: getattr(socket, name) for name in _MODULE_PATCHES}
    orig_methods = {name: getattr(socket.socket, name) for name in _METHOD_PATCHES}

    def _connect(self: socket.socket, address: Any) -> Any:
        if _address_is_local(self, address):
            return orig_methods["connect"](self, address)
        _refuse("an outbound connect", address)

    def _connect_ex(self: socket.socket, address: Any) -> Any:
        # Returns an errno rather than raising, so it reached none of the
        # arms `connect` goes through and left the block as a plain `0`.
        if _address_is_local(self, address):
            return orig_methods["connect_ex"](self, address)
        _refuse("an outbound connect_ex", address)

    def _sendto(self: socket.socket, *args: Any) -> Any:
        # `sendto(data, address)` and `sendto(data, flags, address)`: the
        # destination is the last positional either way, and a datagram never
        # connects, so nothing else in this file would have seen it.
        if args and _address_is_local(self, args[-1]):
            return orig_methods["sendto"](self, *args)
        _refuse("an outbound datagram", args[-1] if args else None)

    def _sendmsg(self: socket.socket, buffers: Any, ancdata: Any = (),
                 flags: int = 0, address: Any = None) -> Any:
        if address is None or _address_is_local(self, address):
            return orig_methods["sendmsg"](self, buffers, ancdata, flags, address)
        _refuse("an outbound datagram", address)

    def _getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        if host is not None and not _is_local(host):
            _refuse("a DNS lookup", (host, port))
        return orig["getaddrinfo"](host, port, *args, **kwargs)

    def _one_arg_resolver(name: str):
        def _resolve(host: Any, *args: Any, **kwargs: Any) -> Any:
            if host is not None and not _is_local(host):
                _refuse("a DNS lookup", host)
            return orig[name](host, *args, **kwargs)
        return _resolve

    def _getnameinfo(sockaddr: Any, *args: Any, **kwargs: Any) -> Any:
        host = sockaddr[0] if isinstance(sockaddr, tuple) else sockaddr
        if not _is_local(host):
            _refuse("a reverse DNS lookup", sockaddr)
        return orig["getnameinfo"](sockaddr, *args, **kwargs)

    # Keyed off `_MODULE_PATCHES` / `_METHOD_PATCHES` rather than written out,
    # and that is not tidiness. Those two are `hasattr`-filtered because
    # Windows has no `socket.socket.sendmsg`, and a hand-written loop would
    # have called `monkeypatch.setattr` on a name that is not there --
    # `AttributeError` out of an autouse fixture, so every test on every
    # Windows leg, not only the ones that touch a socket.
    replacements = {
        "getaddrinfo": _getaddrinfo,
        "getnameinfo": _getnameinfo,
        "gethostbyname": _one_arg_resolver("gethostbyname"),
        "gethostbyname_ex": _one_arg_resolver("gethostbyname_ex"),
        "gethostbyaddr": _one_arg_resolver("gethostbyaddr"),
    }
    method_replacements = {
        "connect": _connect,
        "connect_ex": _connect_ex,
        "sendto": _sendto,
        "sendmsg": _sendmsg,
    }
    for name in _MODULE_PATCHES:
        monkeypatch.setattr(socket, name, replacements[name])
    for name in _METHOD_PATCHES:
        monkeypatch.setattr(socket.socket, name, method_replacements[name])
