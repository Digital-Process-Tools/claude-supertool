"""#928 pin: HTTP test fixtures must never resolve a name for their loopback
server.

`HTTPServer.server_bind()` calls `socket.getfqdn(host)` -- a reverse DNS
lookup nobody needs, since every fixture in this suite addresses its server
by `127.0.0.1:port`. On a runner with no reverse zone the call blocks for the
full resolver timeout, measured at ~35s per fixture (#928:
test_http_bounds.py, test_security_redirect.py). Locally the same call
returns in 0.01s, so a wall-clock threshold here would never catch a
regression on the one machine most likely to introduce it.

The property that actually matters is behavioural: nothing on this path may
call `socket.getfqdn`. Patch it to explode and prove it never fires.
"""
from __future__ import annotations

import socket
import sys
from http.server import HTTPServer, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from _no_fqdn_server import NoFqdnHTTPServer, NoFqdnThreadingHTTPServer  # noqa: E402


class _Handler:
    """Never instantiated -- server_bind() runs before any request handling."""


def _poison_getfqdn(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*a: object, **kw: object) -> str:
        raise AssertionError("socket.getfqdn was called -- a name resolution happened")

    monkeypatch.setattr(socket, "getfqdn", _boom)


@pytest.mark.parametrize("server_cls", [NoFqdnHTTPServer, NoFqdnThreadingHTTPServer])
def test_server_bind_never_resolves_a_name(server_cls: type, monkeypatch: pytest.MonkeyPatch) -> None:
    _poison_getfqdn(monkeypatch)
    srv = server_cls(("127.0.0.1", 0), _Handler)
    try:
        assert srv.server_name == "127.0.0.1"
        assert srv.server_port > 0
    finally:
        srv.server_close()


@pytest.mark.parametrize("server_cls", [HTTPServer, ThreadingHTTPServer])
def test_stock_http_server_would_have_tripped_the_pin(
    server_cls: type, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proves the pin is not vacuous: the stdlib class it replaces does call
    getfqdn on bind, which is exactly what the fix removes."""
    _poison_getfqdn(monkeypatch)
    with pytest.raises(AssertionError, match="socket.getfqdn was called"):
        server_cls(("127.0.0.1", 0), _Handler)
