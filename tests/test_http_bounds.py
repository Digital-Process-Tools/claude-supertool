"""Preset HTTP must be bounded in bytes and in wall clock, and must not raise
out of a function documented as never raising (#766, items 1-3).

Three defects, one shape: a response that is not what the caller asked for is
handed back — or thrown — as though it were.

1. `resp.read()` is unbounded at every credentialed call site. A hostile or
   merely broken endpoint returns a multi-gigabyte body and the op consumes it
   into memory. The cap must be a **refusal**, not a truncation: a silently
   truncated JSON body becomes a `JSONDecodeError` that reads as "bad JSON from
   Hashnode", which is the wrong diagnosis.
2. urllib's `timeout` is a per-`recv` socket timeout, not a deadline. A server
   that drips one byte at a time resets it forever, so no value of `timeout`
   bounds the call.
3. `http.client.IncompleteRead` subclasses `HTTPException`, not `OSError`, so it
   escapes handlers that promise "returns None on any error".

The servers here are the attack: a loopback origin that answers with an
oversized body, a dripped body, or a body shorter than its own
`Content-Length`. Tokens are obviously fake and are never printed.
"""
from __future__ import annotations

import ast
import http.client
import importlib
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "presets"))
sys.path.insert(0, str(ROOT / "tests"))

from _no_fqdn_server import NoFqdnThreadingHTTPServer  # noqa: E402
from _preset_loader import load_preset_module  # noqa: E402

# Same import discipline as tests/test_security_redirect.py: `_http` plainly, so
# every client shares one module object and one seam; the clients through the
# isolating loader, because a bare import resolves siblings by name collision.
_http = importlib.import_module("_http")
hn_gql = load_preset_module("hashnode", "_graphql", "hb_")
dv_rest = load_preset_module("devto", "_rest", "hb_")
dv_session = load_preset_module("devto", "_session", "hb_")
dv_comment = load_preset_module("devto", "comment", "hb_")
bs = load_preset_module("bluesky", "_atproto", "hb_")

FAKE_HASHNODE_TOKEN = "hn_FAKE_NOT_A_REAL_TOKEN_0001"
FAKE_DEVTO_KEY = "devto_FAKE_NOT_A_REAL_KEY_0002"
FAKE_DEVTO_COOKIE = "_forem_user=FAKE_NOT_A_REAL_SESSION_0003"
FAKE_JWT = "FAKE_NOT_A_REAL_JWT_0004"
FAKE_APP_PASSWORD = "fake-app-pass-0005"

SENTINEL = "SHOULD_NOT_BE_PARSED"
TEST_CAP = 2048


def _json_body(pad: int) -> bytes:
    """Valid JSON, parseable if it ever reached a client, and self-identifying."""
    return (
        b'{"data": {"pad": "' + b"A" * pad + b'", "sentinel": "' + SENTINEL.encode() + b'"}}'
    )


# ---------------------------------------------------------------------------
# Loopback origins
# ---------------------------------------------------------------------------

class _Body(BaseHTTPRequestHandler):
    """Answers with the body its server was configured with, in one of four ways.

    plain            - honest Content-Length, sent at once
    close_delimited  - HTTP/1.0, no Content-Length, EOF ends the body (the
                       streaming branch: the cap cannot be pre-checked)
    truncate         - declares more bytes than it sends, then closes
                       (the IncompleteRead shape)
    drip             - honest Content-Length, one byte at a time (the slowloris
                       shape: every recv succeeds, so the socket timeout never
                       fires)
    """

    protocol_version = "HTTP/1.1"

    def _run(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        cfg = self.server.cfg
        body = cfg["body"]
        mode = cfg["mode"]
        try:
            if mode == "close_delimited":
                self.protocol_version = "HTTP/1.0"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
                self.close_connection = True
                return
            if mode == "truncate":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body) + 4096))
                self.end_headers()
                self.wfile.write(body)
                self.close_connection = True
                return
            if mode == "drip":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                for i in range(len(body)):
                    self.wfile.write(body[i:i + 1])
                    self.wfile.flush()
                    time.sleep(cfg["delay"])
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            # The client hung up mid-answer, which is the point of every test
            # here. Without this the socketserver error handler prints a
            # traceback onto the stderr the tests assert on.
            self.close_connection = True

    do_GET = do_POST = _run

    def log_message(self, *args) -> None:
        pass


def _serve(mode: str, body: bytes, delay: float = 0.0) -> NoFqdnThreadingHTTPServer:
    srv = NoFqdnThreadingHTTPServer(("127.0.0.1", 0), _Body)
    srv.cfg = {"mode": mode, "body": body, "delay": delay}
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def big(monkeypatch):
    """An origin answering a body four times the (lowered) cap."""
    monkeypatch.setattr(_http, "MAX_RESPONSE_BYTES", TEST_CAP)
    srv = _serve("plain", _json_body(TEST_CAP * 4))
    srv.base = f"http://127.0.0.1:{srv.server_port}"
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def truncated():
    """An origin that declares more bytes than it sends, then closes."""
    srv = _serve("truncate", _json_body(16))
    srv.base = f"http://127.0.0.1:{srv.server_port}"
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def drip():
    """An origin dripping one byte at a time: every recv succeeds forever."""
    srv = _serve("drip", _json_body(400), delay=0.05)
    srv.base = f"http://127.0.0.1:{srv.server_port}"
    yield srv
    srv.shutdown()
    srv.server_close()


def assert_capped(capsys) -> str:
    """The op refused, said why, and did not hand the body on."""
    out = capsys.readouterr()
    both = out.out + out.err
    assert SENTINEL not in both, "the oversized body reached the caller's output"
    assert "too large" in both.lower(), f"the cap refusal was not disclosed: {both!r}"
    return both


# ---------------------------------------------------------------------------
# 1. The cap is a refusal, not a truncation
# ---------------------------------------------------------------------------

def test_read_capped_refuses_an_oversized_body(big) -> None:
    """The bar: this must fail if `read_capped` truncates instead of refusing."""
    with _http.urlopen(big.base + "/x", timeout=5) as resp:
        with pytest.raises(_http.ResponseTooLarge) as exc:
            _http.read_capped(resp, limit=TEST_CAP)
    assert SENTINEL not in str(exc.value), "the refusal echoed the body it refused"
    assert str(TEST_CAP) in str(exc.value), "the refusal does not name the cap it enforced"


def test_read_capped_returns_a_body_inside_the_cap() -> None:
    """The guard is not 'no responses': an honest body still comes back whole."""
    srv = _serve("plain", _json_body(16))
    try:
        with _http.urlopen(f"http://127.0.0.1:{srv.server_port}/x", timeout=5) as resp:
            body = _http.read_capped(resp, limit=TEST_CAP)
        assert body == _json_body(16)
    finally:
        srv.shutdown()
        srv.server_close()


def test_read_capped_refuses_a_close_delimited_oversized_body() -> None:
    """No Content-Length to pre-check, so the cap has to hold while streaming."""
    srv = _serve("close_delimited", _json_body(TEST_CAP * 4))
    try:
        with _http.urlopen(f"http://127.0.0.1:{srv.server_port}/x", timeout=5) as resp:
            with pytest.raises(_http.ResponseTooLarge):
                _http.read_capped(resp, limit=TEST_CAP)
    finally:
        srv.shutdown()
        srv.server_close()


def test_response_too_large_is_not_an_oserror() -> None:
    """`gql_safe`-style helpers catch OSError and return None. An abusive body
    must not disappear into that silent path — the whole point of the cap is
    that the operator gets told."""
    assert not issubclass(_http.ResponseTooLarge, OSError)


def test_hashnode_gql_refuses_an_oversized_body(big, capsys, monkeypatch) -> None:
    monkeypatch.setattr(hn_gql, "ENDPOINT", big.base + "/gql")
    with pytest.raises(SystemExit) as exc:
        hn_gql.gql("query { x }", {}, FAKE_HASHNODE_TOKEN, timeout=5)
    assert exc.value.code != 0
    assert_capped(capsys)


def test_hashnode_gql_safe_refuses_instead_of_returning_none(big, capsys, monkeypatch) -> None:
    """gql_safe degrades to None on a failed lookup. A body four times the cap is
    not a failed lookup — nothing about it should be quiet."""
    monkeypatch.setattr(hn_gql, "ENDPOINT", big.base + "/gql")
    with pytest.raises(SystemExit) as exc:
        hn_gql.gql_safe("query { x }", {}, FAKE_HASHNODE_TOKEN, timeout=5)
    assert exc.value.code != 0
    assert_capped(capsys)


def test_devto_rest_refuses_an_oversized_body(big, capsys, monkeypatch) -> None:
    monkeypatch.setattr(dv_rest, "BASE", big.base + "/api")
    with pytest.raises(SystemExit) as exc:
        dv_rest.request("GET", "/articles/1", FAKE_DEVTO_KEY, timeout=5)
    assert exc.value.code != 0
    assert_capped(capsys)


def test_devto_session_csrf_refuses_an_oversized_body(big, capsys, monkeypatch) -> None:
    """The worst of the call sites: it decodes the whole thing to run one regex."""
    monkeypatch.setattr(dv_session, "WEB_BASE", big.base)
    with pytest.raises(SystemExit) as exc:
        dv_session.fetch_csrf_token(FAKE_DEVTO_COOKIE, timeout=5)
    assert exc.value.code != 0
    assert_capped(capsys)


def test_devto_session_post_refuses_an_oversized_body(big, capsys, monkeypatch) -> None:
    monkeypatch.setattr(dv_session, "WEB_BASE", big.base)
    with pytest.raises(SystemExit) as exc:
        dv_session.web_post_json("/reactions", FAKE_DEVTO_COOKIE, "fake-csrf", {"x": 1}, timeout=5)
    assert exc.value.code != 0
    assert_capped(capsys)


def test_bluesky_xrpc_refuses_an_oversized_body(big, capsys, monkeypatch) -> None:
    monkeypatch.setattr(bs, "PDS", big.base)
    with pytest.raises(SystemExit) as exc:
        bs.xrpc("app.bsky.feed.getTimeline", {"accessJwt": FAKE_JWT}, timeout=5)
    assert exc.value.code != 0
    assert_capped(capsys)


def test_bluesky_create_session_refuses_an_oversized_body(
    big, capsys, monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(bs, "PDS", big.base)
    monkeypatch.setattr(bs, "SESSION_FILE", tmp_path / "session.json")
    with pytest.raises(SystemExit) as exc:
        bs.create_session("fake.handle.invalid", FAKE_APP_PASSWORD)
    assert exc.value.code != 0
    assert_capped(capsys)


def test_bluesky_refresh_session_refuses_an_oversized_body(
    big, capsys, monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(bs, "PDS", big.base)
    monkeypatch.setattr(bs, "SESSION_FILE", tmp_path / "session.json")
    with pytest.raises(SystemExit) as exc:
        bs.refresh_session(FAKE_JWT)
    assert exc.value.code != 0
    assert_capped(capsys)


def test_devto_comment_resolver_gives_up_on_an_oversized_body(
    big, capsys, monkeypatch
) -> None:
    """No credential rides on this hop, so it keeps its best-effort contract and
    returns None — but it says the body was refused rather than looking like an
    ordinary miss, exactly as it does for a refused redirect."""
    monkeypatch.setattr(dv_comment, "API_BASE", big.base + "/api")
    assert dv_comment._resolve_parent_numeric_id("abc123", timeout=5) is None
    assert_capped(capsys)


# ---------------------------------------------------------------------------
# 2. A deadline, not a per-recv socket timeout
# ---------------------------------------------------------------------------

def test_a_dripping_server_is_bounded_by_the_deadline(drip) -> None:
    """Every recv succeeds, so `timeout` never fires. Only wall clock ends this.

    The measurement in #766: a drip server at one byte per 0.5s kept a
    `timeout=1` call running for 4.7 seconds, and no value of `timeout` bounds
    it. The body here is 400+ bytes at 0.05s each — 20+ seconds if unbounded.
    """
    started = time.monotonic()
    with pytest.raises(_http.DeadlineExceeded) as exc:
        with _http.urlopen(drip.base + "/x", timeout=5, deadline=1.0) as resp:
            _http.read_capped(resp)
    elapsed = time.monotonic() - started
    assert elapsed < 4.0, f"the deadline did not bound the read: {elapsed:.1f}s"
    assert "1" in str(exc.value), "the timeout message does not name the deadline"


def test_deadline_exceeded_is_a_timeout_error() -> None:
    """A deadline miss is a lookup that failed, not a response that lied. The
    helpers documented as degrading to None already catch OSError, and this is
    the failure they exist for — it belongs inside that contract, not outside."""
    assert issubclass(_http.DeadlineExceeded, TimeoutError)


def test_hashnode_gql_reports_a_deadline_instead_of_a_traceback(drip, capsys, monkeypatch) -> None:
    """No client grew a second timeout argument: the deadline is a multiple of
    the per-operation timeout the caller already passes, so every call site
    inherits a bound without being edited to ask for one."""
    monkeypatch.setattr(_http, "DEADLINE_FACTOR", 0.2)  # timeout=5 -> a 1s deadline
    monkeypatch.setattr(hn_gql, "ENDPOINT", drip.base + "/gql")
    with pytest.raises(SystemExit) as exc:
        hn_gql.gql("query { x }", {}, FAKE_HASHNODE_TOKEN, timeout=5)
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "ERROR" in err and "deadline" in err.lower(), f"unhelpful: {err!r}"


def test_hashnode_gql_safe_degrades_on_a_deadline(drip, monkeypatch) -> None:
    """The other half of the contract: a slow endpoint is a lookup that failed,
    so it degrades to None rather than stopping the run."""
    monkeypatch.setattr(_http, "DEADLINE_FACTOR", 0.2)
    monkeypatch.setattr(hn_gql, "ENDPOINT", drip.base + "/gql")
    assert hn_gql.gql_safe("query { x }", {}, FAKE_HASHNODE_TOKEN, timeout=5) is None


# ---------------------------------------------------------------------------
# 3. The "returns None on any error" contract, stated once and kept
# ---------------------------------------------------------------------------

def test_a_short_body_is_an_error_not_a_short_body(truncated) -> None:
    """A body shorter than its own Content-Length must raise, never return.

    This is the same rule as the cap, from the other end: handing back the bytes
    that did arrive turns a transport failure into "bad JSON from Hashnode",
    which sends the reader to the wrong file.
    """
    with _http.urlopen(truncated.base + "/x", timeout=5) as resp:
        with pytest.raises(http.client.IncompleteRead):
            _http.read_capped(resp)


def test_incomplete_read_does_not_escape_gql_safe(truncated, monkeypatch) -> None:
    """`IncompleteRead` subclasses `HTTPException`, not `OSError`, so the
    documented handler tuple never caught it and it propagated out of a function
    whose docstring promises it does not raise."""
    monkeypatch.setattr(hn_gql, "ENDPOINT", truncated.base + "/gql")
    assert hn_gql.gql_safe("query { x }", {}, FAKE_HASHNODE_TOKEN, timeout=5) is None


def test_incomplete_read_does_not_escape_refresh_session(
    truncated, monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(bs, "PDS", truncated.base)
    monkeypatch.setattr(bs, "SESSION_FILE", tmp_path / "session.json")
    assert bs.refresh_session(FAKE_JWT) is None


def test_incomplete_read_is_reported_not_traced_by_the_exiting_clients(
    truncated, capsys, monkeypatch
) -> None:
    monkeypatch.setattr(hn_gql, "ENDPOINT", truncated.base + "/gql")
    with pytest.raises(SystemExit) as exc:
        hn_gql.gql("query { x }", {}, FAKE_HASHNODE_TOKEN, timeout=5)
    assert exc.value.code != 0
    assert "ERROR" in capsys.readouterr().err


def test_the_narrowing_carve_outs_are_documented_where_they_are_promised() -> None:
    """#761 carved `RedirectRefused` out of the "returns None on any error"
    contract and #766 adds `ResponseTooLarge`. Two exceptions to a documented
    promise that the docstring does not name is how the promise stops being
    read."""
    for doc in (hn_gql.gql_safe.__doc__ or "", bs.refresh_session.__doc__ or ""):
        assert "RedirectRefused" in doc, "the redirect carve-out is not stated"
        assert "ResponseTooLarge" in doc, "the size carve-out is not stated"


# ---------------------------------------------------------------------------
# 4. Structural: the cap must be wired at every call site, not merely exist
# ---------------------------------------------------------------------------

def _http_response_names(tree: ast.AST) -> set[str]:
    """Names in this module that are bound to an HTTP response or error body:
    `with urlopen(...) as N`, `except HTTPError as N`, and parameters annotated
    as an HTTPError (the `_format_http_error(e)` helpers)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.With):
            for item in node.items:
                call = item.context_expr
                if not isinstance(call, ast.Call) or item.optional_vars is None:
                    continue
                fn = call.func
                fn_name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                if fn_name == "urlopen" and isinstance(item.optional_vars, ast.Name):
                    names.add(item.optional_vars.id)
        elif isinstance(node, ast.ExceptHandler) and node.name and node.type is not None:
            if "HTTPError" in ast.unparse(node.type):
                names.add(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in node.args.args:
                if arg.annotation is not None and "HTTPError" in ast.unparse(arg.annotation):
                    names.add(arg.arg)
    return names


def test_no_unbounded_response_reads_remain_under_presets() -> None:
    """The headline failure mode of this repo is a guard that exists and is not
    wired everywhere — #761 added the same test for `urllib.request.urlopen(`.
    An argument-less `.read()` on a response or an error body is unbounded, so a
    fifth integration that types one re-earns the defect. Fail here instead."""
    offenders = []
    for path in sorted((ROOT / "presets").rglob("*.py")):
        if path.name == "_http.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = _http_response_names(tree)
        if not names:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or node.args or node.keywords:
                continue
            fn = node.func
            if (
                isinstance(fn, ast.Attribute)
                and fn.attr == "read"
                and isinstance(fn.value, ast.Name)
                and fn.value.id in names
            ):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert offenders == [], (
        "these reads are unbounded — use _http.read_capped() for a response body, "
        f"or pass an explicit byte limit for an error body: {offenders}"
    )
