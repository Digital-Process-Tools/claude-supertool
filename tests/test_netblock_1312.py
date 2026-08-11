"""The outbound guard's own contract (#1312).

`tests/_netblock.py` exists because three tests reached third-party hosts and
passed on whatever came back. A guard against that class has to survive the
class: the properties below are the ones whose absence would let it fail
silently, which is the failure mode it was written to remove.
"""
from __future__ import annotations

import socket

import pytest

from _netblock import LOOPBACK, OutboundBlocked, block_outbound


def test_a_non_loopback_lookup_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    block_outbound(monkeypatch)
    with pytest.raises(OutboundBlocked) as exc:
        socket.getaddrinfo("example.invalid", 443)
    # The message has to name the destination -- a refusal that does not say
    # where the test was going leaves the reader to guess which stub is missing.
    assert "example.invalid" in str(exc.value)
    assert "_OPEN" in str(exc.value)


def test_a_non_loopback_connect_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    block_outbound(monkeypatch)
    s = socket.socket()
    try:
        with pytest.raises(OutboundBlocked):
            s.connect(("93.184.216.34", 443))
    finally:
        s.close()


def test_a_broad_except_cannot_absorb_the_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """The property that makes the guard trustworthy rather than decorative.

    Presets catch broadly on purpose -- `gql_safe` swallows every `OSError` to
    return `None`, and `hashnode/comment.py::main` renders a bare
    `except Exception` into an `ABORT -- pre-flight failed (...)` line. If the
    refusal were an ordinary `Exception` it would land inside one of those arms
    and be printed as a plausible product verdict, and a test asserting
    `"ABORT" in err` would go green on the guard firing. That is #1312 exactly,
    one layer up, so it is pinned rather than left to a base class nobody
    re-reads.
    """
    block_outbound(monkeypatch)
    swallowed = False
    try:
        try:
            socket.getaddrinfo("example.invalid", 443)
        except Exception:          # noqa: BLE001 - the point of the test
            swallowed = True
        except OSError:            # pragma: no cover - unreachable, kept explicit
            swallowed = True
    except OutboundBlocked:
        pass
    assert not swallowed, "a bare `except Exception` absorbed the guard's refusal"


@pytest.mark.parametrize("host", sorted(LOOPBACK))
def test_loopback_is_not_refused(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    """A guard that blocks loopback would break the suites that bind real
    servers (`test_http_bounds.py`, the claude-channel suites) and would read
    exactly like a true egress finding.

    The assertion is *only* that the guard declines to refuse. `socket.gaierror`
    is caught and ignored on purpose: whether a given runner's resolver answers
    for `::1` or `localhost` is a property of that runner -- Windows and
    container legs differ -- and asserting on it would make this test fail for
    an environmental reason, which is the whole shape #1312 is about.
    """
    block_outbound(monkeypatch)
    try:
        socket.getaddrinfo(host or None, 0)
    except OutboundBlocked:
        raise
    except socket.gaierror:
        pass


def test_the_patch_is_undone_after_the_test() -> None:
    """`monkeypatch` restores both seams. Without this the guard would leak into
    every test that follows in the same xdist worker, and the resulting failures
    would name hosts belonging to tests that never armed it."""
    before_connect = socket.socket.connect
    before_getaddrinfo = socket.getaddrinfo
    with pytest.MonkeyPatch.context() as mp:
        block_outbound(mp)
        assert socket.getaddrinfo is not before_getaddrinfo
    assert socket.socket.connect is before_connect
    assert socket.getaddrinfo is before_getaddrinfo
