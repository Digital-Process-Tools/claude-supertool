"""The transport seam is enforced by the socket, not by a convention (#1341).

`presets/_http.py` binds `_OPEN = _OPENER.open` at import, and every preset
calls through that name. A test that stubs `MODULE.urllib.request.urlopen`
therefore replaces a name **nothing consults**: the request goes out to the real
host and the test passes on whatever the internet answers. Two tests in
`tests/test_security_error_echo_691.py` were in that state for months, one of
them a credential-redaction regression test whose injected payload had never
once been delivered. #1312 repointed them.

Why repointing was not the fix
------------------------------
`monkeypatch.setattr` and `mock.patch` both raise on a name that does not exist,
so *renaming* `_OPEN` would already redden its stubs. The defect was the
opposite shape and no rename check reaches it: the stubbed name **existed** and
was live -- `urllib.request.urlopen` is perfectly real -- it just was not the
name the product calls. Only observing the transport can tell those apart.

What is enforced now, and how
-----------------------------
`tests/conftest.py` arms `_netblock.block_outbound` for **every** test. A stub
against the wrong name no longer succeeds quietly against a third-party host; it
fails at `connect`/`getaddrinfo`, naming the destination and what to stub.

#1312 measured both methods over 559 test modules. A static grep for transport
tokens found 2 of the 3 live leaks. The socket recorder found 3 of 3 -- the one
it alone caught (`bsky.social`) contained no transport token at all, because it
was a *missing* stub rather than a wrong one. So the static mirror scan the
issue proposed is deliberately not shipped here: it is strictly weaker than what
this file arms, and a second guard whose green means less than the first one's
is how a green stops meaning anything.

The blind spot, stated rather than discovered later
---------------------------------------------------
This blocks sockets **in the pytest process only**. A test that shells out to
`supertool.py` or a preset as a subprocess is not covered -- the child has its
own unpatched `socket` module. Loopback and `AF_UNIX` stay open on purpose:
`test_http_bounds.py` and the `claude-channel` suites bind real servers on
`127.0.0.1`, and those are hermetic because the process under test is the one
that answered.
"""
from __future__ import annotations

import importlib
import inspect
import socket
import sys
import urllib.request
from pathlib import Path

import conftest
import pytest

from _netblock import OutboundBlocked

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "presets"))

_http = importlib.import_module("_http")


def test_the_suite_arms_the_outbound_block_without_being_asked() -> None:
    """No `block_outbound(monkeypatch)` call in this test, on purpose.

    The bar: this must fail if conftest does nothing. Before #1341 the guard
    existed and had to be opted into per test, which is the same shape as a
    linter nobody runs.
    """
    with pytest.raises(OutboundBlocked) as exc:
        socket.getaddrinfo("no-such-host.invalid", 443)
    assert "no-such-host.invalid" in str(exc.value), (
        "the refusal must name the destination, or it reads as an ordinary "
        "DNS failure -- which is the state #1312 found"
    )


def test_stubbing_the_dead_name_now_fails_instead_of_reaching_the_internet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1312's defect, reproduced as a pin.

    `urllib.request.urlopen` is stubbed -- a real, live, importable name -- and
    the preset transport is then driven. `_http.urlopen` goes through `_OPEN`,
    never through the stub, so before this guard the call left the machine and
    the test passed on the reply. Now it stops at the socket.
    """
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda *a, **k: pytest.fail("the dead name was called after all"),
    )
    with pytest.raises(OutboundBlocked):
        _http.urlopen("https://example.invalid/x", timeout=5)


def test_stubbing_the_live_seam_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard must not make correct stubbing impossible.

    Patching `_http._OPEN` -- the name the product really calls -- reaches no
    socket at all, so the block never fires. This is the assertion that stops
    the guard being read as "the suite may not touch HTTP".
    """
    seen = {}

    def fake_open(req, timeout=None):
        seen["url"] = req.full_url if hasattr(req, "full_url") else str(req)
        raise RuntimeError("stub reached")

    monkeypatch.setattr(_http, "_OPEN", fake_open)
    with pytest.raises(RuntimeError, match="stub reached"):
        _http.urlopen("https://example.invalid/x", timeout=5)
    assert seen["url"] == "https://example.invalid/x"


def test_the_guard_fixture_does_not_take_the_shared_monkeypatch() -> None:
    """Not a style pin -- an ordering one, and it cost a full-suite run to find.

    `_block_outbound_network` is autouse and defined near the top of
    `conftest.py`, so it is set up first and torn down **last**. Requesting the
    shared `monkeypatch` fixture from it instantiates `monkeypatch` at that
    point, which moves `monkeypatch.undo()` to after `_guard_repo_git_state`'s
    teardown. Six tests in `test_git_resolve.py` patch `os.path.isfile` to
    `lambda p: True`; CPython 3.13+ routes `Path.is_file()` through
    `os.path.isfile`; the git guard's after-snapshot then read every directory
    under `refs/heads/` as an unreadable ref file and errored the teardown of
    tests that had changed nothing -- 19 such errors on the first full run.

    A private `pytest.MonkeyPatch.context()` cannot reorder anything, which is
    why the fixture builds its own.
    """
    fixture = getattr(conftest._block_outbound_network, "__wrapped__",
                      conftest._block_outbound_network)
    params = set(inspect.signature(fixture).parameters)
    assert "monkeypatch" not in params, (
        "the suite-wide network guard requests the shared `monkeypatch` "
        "fixture again; that reorders its undo past the git-state guard "
        f"(#1341). Parameters: {sorted(params)}"
    )


def test_loopback_is_still_reachable() -> None:
    """Suite-wide arming must not break the hermetic loopback servers."""
    srv = socket.socket()
    try:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        client = socket.socket()
        try:
            client.connect(srv.getsockname())
        finally:
            client.close()
    finally:
        srv.close()
