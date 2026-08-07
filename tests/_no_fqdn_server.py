"""Server classes for test fixtures that bind loopback and never resolve a name.

`HTTPServer.server_bind()` does:

    host, port = self.server_address[:2]
    self.server_name = socket.getfqdn(host)

`socket.getfqdn()` is a reverse DNS lookup. Every fixture in this suite
addresses its server by `127.0.0.1:port`, so `server_name` is dead weight --
but on a runner whose resolver has no reverse zone (or is slow/unreachable),
that call blocks until the resolver gives up. Measured at ~35s per fixture
(#928), invisible on a dev machine where the same call returns in 0.01s.

Both classes below are behaviourally identical to their stdlib counterparts
-- same bind, same threading model -- with `server_bind` overridden to run
only `socketserver.TCPServer.server_bind`, which is the part of
`HTTPServer.server_bind` that does the actual bind/listen. The getfqdn call
is the only thing skipped.
"""
from __future__ import annotations

import socketserver
from http.server import HTTPServer, ThreadingHTTPServer


class _NoFqdnBind:
    """Mixin: bind without resolving the bound host's name."""

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class NoFqdnHTTPServer(_NoFqdnBind, HTTPServer):
    """HTTPServer that never calls socket.getfqdn() on bind."""


class NoFqdnThreadingHTTPServer(_NoFqdnBind, ThreadingHTTPServer):
    """ThreadingHTTPServer that never calls socket.getfqdn() on bind."""
