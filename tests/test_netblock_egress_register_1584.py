"""#1584 -- the suite-wide netblock's coverage boundary, measured not asserted.

`_netblock.block_outbound` is the sole suite-wide egress gate since #1341, and
until this file it patched `socket.socket.connect` and `socket.getaddrinfo` and
nothing else. Both `conftest.py` and `docs/contributing.md` documented its limit
as exactly **one** thing -- "it binds `socket` in the pytest process only" --
which reads as a closed list. Measured from inside an armed context it was not:

    connect        blocked
    connect_ex     NOT BLOCKED -> 0
    getaddrinfo    blocked
    gethostbyname  NOT BLOCKED -> '104.20.23.154'
    udp sendto     NOT BLOCKED -> 1

`gethostbyname` returned a live-resolved address: real DNS egress from inside
the block. Three more the issue did not name -- `gethostbyname_ex`,
`getnameinfo` and `gethostbyaddr` -- were open the same way, which is the
argument against the fix being "patch three more names": a longer list is still
a list, and the next reader trusts it exactly as much.

So the boundary is a register rather than a sentence. `_netblock.ROUTES`
classifies **every callable the `socket` module exposes** on the running
interpreter, and the tests below fail when a name appears that nobody has
classified. A route that cannot be covered is `OPEN` and carries its reason;
that is the third state, not a shorter list.
"""
from __future__ import annotations

import socket
import sys
import tempfile
from pathlib import Path

import pytest

import _netblock


#: Parametrised rather than looped, so a failure names the route in the test id.
#: Asserted non-empty at collection because a `parametrize` over an empty list
#: is a green leg that ran nothing -- this repo's own defect class, in the file
#: whose subject is that defect class.
PLATFORM_ONLY_NAMES = sorted(_netblock.PLATFORM_ONLY)
assert PLATFORM_ONLY_NAMES, "PLATFORM_ONLY is empty: these tests would run nothing"


TEST_NET_1 = "192.0.2.1"  # RFC 5737, guaranteed not routed.
#: RFC 6761 reserves `.invalid`, so a red leg here cannot leave the building
#: even when the route it is probing is genuinely open.
NOWHERE = "no-such-host.invalid"


# ---------------------------------------------------------------------------
# The routes, held against the armed context conftest already installs.
# ---------------------------------------------------------------------------


def _udp():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(0.2)
    return s


def _tcp():
    s = socket.socket()
    s.settimeout(0.2)
    return s


@pytest.mark.parametrize("name,call", [
    ("connect", lambda: _tcp().connect((TEST_NET_1, 80))),
    ("connect_ex", lambda: _tcp().connect_ex((TEST_NET_1, 80))),
    ("sendto", lambda: _udp().sendto(b"x", (TEST_NET_1, 53))),
    pytest.param(
        "sendmsg", lambda: _udp().sendmsg([b"x"], [], 0, (TEST_NET_1, 53)),
        marks=pytest.mark.skipif(
            not hasattr(socket.socket, "sendmsg"),
            reason="no socket.socket.sendmsg on this platform (Windows)"),
    ),
    ("getaddrinfo", lambda: socket.getaddrinfo(NOWHERE, 80)),
    ("gethostbyname", lambda: socket.gethostbyname(NOWHERE)),
    ("gethostbyname_ex", lambda: socket.gethostbyname_ex(NOWHERE)),
    ("gethostbyaddr", lambda: socket.gethostbyaddr(TEST_NET_1)),
    ("getnameinfo", lambda: socket.getnameinfo((TEST_NET_1, 80), 0)),
])
def test_every_route_classified_as_patched_actually_refuses(name, call) -> None:
    """The autouse gate is already armed; these are held against it directly.

    A route that raises anything other than `OutboundBlocked` reached the
    network stack -- `gaierror` for a name that does not resolve is still the
    resolver having been asked, and on a route that *is* open the same call
    returns an address.
    """
    register = dict(_netblock.ROUTES, **_netblock.SOCKET_ROUTES)
    assert register[name] == _netblock.PATCHED, name
    with pytest.raises(_netblock.OutboundBlocked):
        call()


@pytest.mark.parametrize("call", [
    lambda: socket.getaddrinfo("127.0.0.1", 0),
    lambda: socket.gethostbyname("localhost"),
    # `flags=0` here used to mean a full reverse lookup for "127.0.0.1", and
    # that lookup is what this parameter actually pays for, not what it
    # asserts (#2227): on a GitHub runner it costs 35s to give up before the
    # `except OSError: pass` below ever runs, because the guard's own refusal
    # decision (below, keyed on the HOST) is made before the real resolver is
    # ever reached -- the flags never influence it. `NI_NUMERICHOST` skips the
    # reverse lookup and returns the address literally, still through the
    # patched `getnameinfo`, still proving loopback is not refused, with no
    # resolver round trip to wait on at all.
    lambda: socket.getnameinfo(("127.0.0.1", 80), socket.NI_NUMERICHOST),
])
def test_loopback_is_still_open_on_the_widened_routes(call) -> None:
    """Widening the block must not close the hermetic servers the suite binds.

    `test_http_bounds.py` and the `claude-channel` suites answer themselves on
    `127.0.0.1` and AF_UNIX; a guard that refused those would be trading a
    quiet leak for a loud false finding.

    Only `OutboundBlocked` is a failure here. A runner whose reverse lookup for
    `127.0.0.1` fails raises `gaierror`, which is that runner's resolver and not
    this guard -- asserting "did not raise at all" would turn a host
    configuration into a finding about the diff. `NI_NUMERICHOST` on the third
    parameter mostly forecloses that path already (#2227), and the `except
    OSError: pass` below stays regardless, since `getaddrinfo`/`gethostbyname`
    still make a real -- if local and fast -- resolver call.
    """
    try:
        call()
    except _netblock.OutboundBlocked:
        raise
    except OSError:
        pass


def test_an_af_unix_datagram_is_not_refused() -> None:
    """AF_UNIX has no destination the guard could be protecting.

    Not on `tmp_path`: `sun_path` is 104 bytes on macOS and pytest's per-test
    directory alone is longer than that, so the bind fails with an `OSError`
    that reads exactly like the refusal this test exists to rule out.
    """
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("AF_UNIX is POSIX-only")
    tmp = Path(tempfile.mkdtemp(prefix="nb"))
    peer = tmp / "s"
    if len(str(peer).encode()) > 100:
        pytest.skip("sun_path is too short for this temporary directory")
    try:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        server.bind(str(peer))
    except (OSError, AttributeError) as exc:
        # Windows has `AF_UNIX` but no datagram support for it, and refuses
        # here with an `OSError` that is not this guard talking.
        pytest.skip("AF_UNIX datagrams unavailable: {0}".format(exc))
    client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        client.sendto(b"x", str(peer))
        assert server.recv(8) == b"x"
    finally:
        client.close()
        server.close()


# ---------------------------------------------------------------------------
# The register: the boundary has to be derived from the interpreter, not typed.
# ---------------------------------------------------------------------------


def _module_callables() -> set:
    return {n for n in dir(socket)
            if not n.startswith("_")
            and callable(getattr(socket, n))
            and not isinstance(getattr(socket, n), type)}


def _socket_methods() -> set:
    return {n for n in dir(socket.socket) if not n.startswith("_")}


def test_no_socket_callable_is_left_unclassified() -> None:
    """A route that arrives in a later Python goes red rather than unseen.

    This is the whole point of the register and the one thing a prose note
    cannot do: `recv_fds` and `send_fds` both arrived after the guard's own
    defect class was filed, and either could have been an egress route nobody
    re-read the sentence for.
    """
    unclassified = _module_callables() - set(_netblock.ROUTES)
    assert unclassified == set(), sorted(unclassified)


def test_no_socket_method_is_left_unclassified() -> None:
    unclassified = _socket_methods() - set(_netblock.SOCKET_ROUTES)
    assert unclassified == set(), sorted(unclassified)


def test_the_register_only_classifies_names_that_exist() -> None:
    """A register that outlives its names records coverage it does not have.

    Tolerant of the platform-conditional ones on purpose -- `sethostname` and
    the `if_*` family are not everywhere -- because a name absent here is a
    route that cannot be taken, which is the safe direction. What is not
    tolerated is a name in the register that no Python has.
    """
    stale = set(_netblock.ROUTES) - _module_callables() - _netblock.PLATFORM_OPTIONAL
    assert stale == set(), sorted(stale)
    # `SOCKET_ROUTES` was outside this check until #1642 -- the same half-derived
    # shape as the bug that file exists for, one register over: a method name no
    # Python has could sit there forever and read as coverage.
    stale = set(_netblock.SOCKET_ROUTES) - _socket_methods() - _netblock.PLATFORM_OPTIONAL
    assert stale == set(), sorted(stale)


def test_every_patched_route_is_one_block_outbound_actually_patches() -> None:
    """The register is derived from the code, not maintained alongside it.

    Intersected with what this interpreter has, because Windows has no
    `socket.socket.sendmsg`: claiming it there would be the register doing
    exactly what the prose note did, one layer in.
    """
    declared = {n for n, c in _netblock.ROUTES.items() if c == _netblock.PATCHED}
    declared |= {n for n, c in _netblock.SOCKET_ROUTES.items()
                 if c == _netblock.PATCHED}
    available = _module_callables() | _socket_methods()
    assert declared & available == set(_netblock.PATCHES), (
        sorted((declared & available) ^ set(_netblock.PATCHES)))
    assert declared - available <= _netblock.PLATFORM_OPTIONAL, (
        sorted(declared - available - _netblock.PLATFORM_OPTIONAL))


def test_block_outbound_patches_only_names_this_interpreter_has() -> None:
    """The patch loop must iterate the derived tuples, not a written-out list.

    `_MODULE_PATCHES` / `_METHOD_PATCHES` are `hasattr`-filtered because
    Windows has no `socket.socket.sendmsg`. A loop that names the four methods
    inline passes everywhere the author can run it and calls
    `monkeypatch.setattr` on a missing attribute on Windows -- `AttributeError`
    out of an autouse fixture, so every test on every Windows leg rather than
    the ones that touch a socket.
    """
    if "sendmsg" not in _netblock._METHOD_PATCHES:
        pytest.skip("this interpreter already has no sendmsg")
    # Not `delattr`: `socket.socket.sendmsg` is inherited from `_socket.socket`
    # and cannot be removed from the subclass, so the platform is simulated on
    # the derived tuple -- which is the thing the loop is required to read.
    original = socket.socket.sendmsg
    shortened = tuple(n for n in _netblock._METHOD_PATCHES if n != "sendmsg")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_netblock, "_METHOD_PATCHES", shortened)
        with pytest.MonkeyPatch.context() as inner:
            _netblock.block_outbound(inner)
            assert socket.socket.sendmsg is original, (
                "block_outbound replaced a method that is not in "
                "_METHOD_PATCHES, so it is reading a written-out list; on "
                "Windows that same list names an attribute that is not there")
            with pytest.raises(_netblock.OutboundBlocked):
                _tcp().connect((TEST_NET_1, 80))


@pytest.mark.parametrize("name", PLATFORM_ONLY_NAMES)
def test_every_platform_only_route_is_classified(name) -> None:
    """The population is derived, so it cannot see another platform's names.

    `ioctl` is Windows-only, was in no register, and went red on all four
    Windows legs of PR #1642 while macOS and Linux stayed green -- the derived
    check working exactly as designed and arriving one platform at a time,
    which is the register learning its own boundary from CI. `PLATFORM_ONLY`
    states those names ahead of the leg; this is what makes stating them
    load-bearing rather than a comment.
    """
    register = dict(_netblock.ROUTES, **_netblock.SOCKET_ROUTES)
    assert name in register, name
    assert name in _netblock.PLATFORM_OPTIONAL, name


@pytest.mark.parametrize("name", PLATFORM_ONLY_NAMES)
def test_a_platform_only_route_is_present_exactly_on_its_platform(name) -> None:
    """Turns a reasoned claim into an observed one, on whichever leg can.

    Not vacuous here: on macOS and Linux this asserts every `win32` name is
    absent, which is the half a POSIX author can check. On a Windows leg the
    same assertion runs the other way and observes the presence that this
    file, written on macOS, could only reason about from CPython's source.
    """
    expected = sys.platform.startswith(_netblock.PLATFORM_ONLY[name])
    present = hasattr(socket, name) or hasattr(socket.socket, name)
    assert present == expected, (name, sys.platform, present)


def test_every_open_route_carries_a_reason() -> None:
    """`OPEN` is the third state; without a reason it is just a shorter list."""
    for name, reason in _netblock.OPEN_ROUTES.items():
        assert reason.strip(), name
    named = set(_netblock.OPEN_ROUTES)
    classified = {n for n, c in _netblock.ROUTES.items() if c == _netblock.OPEN}
    classified |= {n for n, c in _netblock.SOCKET_ROUTES.items()
                   if c == _netblock.OPEN}
    assert named == classified, sorted(named ^ classified)


def test_the_uncoverable_routes_are_stated_rather_than_counted_as_blocked(
) -> None:
    """The blind spot was one sentence and is at least four things (#1584).

    Fixed at four so that adding a fifth is a decision somebody records here
    rather than a line appended to prose nobody re-reads.
    """
    assert len(_netblock.BEYOND_THE_PROCESS) == 4, _netblock.BEYOND_THE_PROCESS
    for reason in _netblock.BEYOND_THE_PROCESS:
        assert reason.strip()
