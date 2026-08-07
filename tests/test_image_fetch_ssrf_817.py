"""#817 — `gh-issue` fetched any URL an issue's markdown named, and printed the path.

The attack, verified by hand against `master` before a line was changed:

    ![x](http://169.254.169.254/latest/meta-data/iam/security-credentials/role)

`_extract_image_urls` matched it (any `http(s)` URL, no allowlist),
`_download_images` fetched it with `urllib.request.urlretrieve` — the *default*
opener, so `presets/_http.py`'s `SafeRedirectHandler` never saw it — wrote the
response body to `/tmp/supertool-images/gh/N/`, and the op printed the path
under `## Images`. The agent reading that output opens the file. Blind SSRF
becomes full-read SSRF.

Three separate holes, and the audit named one:

1.  No **destination** policy at all. Routing through `_http.urlopen` alone
    would not have fixed the reported attack: the metadata URL is the *first*
    hop, and the only guard `_http.py` had was a same-origin check on
    *redirects*. `_http.py` needed a policy it did not have, not merely a caller
    that adopted one.
2.  No **redirect** policy — the raw `urlretrieve` followed a cross-origin 302
    that `_http.urlopen` refuses. Verified: `_http.urlopen` raised
    `RedirectRefused` on the same URL that `_download_images` fetched and saved.
3.  No size cap, no deadline, no scheme restriction, no content-type check, and
    a filename taken from `os.path.basename` of attacker text — `basename("..")`
    is `".."`.

The servers here are the attack. Nothing in this file talks to the network.
"""
from __future__ import annotations

import importlib
import re
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "presets"))

from _preset_loader import load_preset_module  # noqa: E402

_http = importlib.import_module("_http")
issue = load_preset_module("github", "issue", "ssrf817_")


# ---------------------------------------------------------------------------
# 1. The address policy — what may be connected to at all
# ---------------------------------------------------------------------------

REFUSED_ADDRESSES = [
    ("169.254.169.254", "the cloud metadata service named in the report"),
    ("169.254.170.2", "the ECS task metadata endpoint"),
    ("127.0.0.1", "loopback"),
    ("127.1.2.3", "loopback, written the way a filter that string-matches misses"),
    ("::1", "IPv6 loopback"),
    ("10.0.0.1", "RFC1918"),
    ("172.16.0.1", "RFC1918, the range most often forgotten"),
    ("192.168.1.1", "RFC1918"),
    ("100.64.0.1", "CGNAT — a shared address, not a public one"),
    ("0.0.0.0", "unspecified"),
    ("fd00::1", "IPv6 unique-local"),
    ("fe80::1", "IPv6 link-local"),
    ("::ffff:127.0.0.1", "IPv4-mapped loopback — a v6 spelling of a v4 secret"),
    ("::ffff:169.254.169.254", "IPv4-mapped metadata service"),
]


@pytest.mark.parametrize("addr,why", REFUSED_ADDRESSES, ids=[a for a, _ in REFUSED_ADDRESSES])
def test_non_public_addresses_are_refused(addr: str, why: str) -> None:
    reason = _http.check_address(addr)
    assert reason is not None, f"{addr} was permitted ({why})"
    assert "not a public" in reason or "multicast" in reason, f"the reason is vague: {reason!r}"


@pytest.mark.parametrize(
    "wrapped,inner",
    [
        ("::ffff:127.0.0.1", "127.0.0.1"),
        ("::ffff:169.254.169.254", "169.254.169.254"),
        ("2002:a00:1::", "10.0.0.1"),          # 6to4 wrapping RFC1918
    ],
)
def test_a_wrapped_v4_address_is_reported_as_the_address_it_wraps(wrapped, inner) -> None:
    """`is_global` on the outer form is not the check — the unwrap is.

    CPython's `IPv6Address.is_global` learned about `ipv4_mapped` recently
    enough that relying on it silently changes this module's behaviour by
    interpreter version, and it has never covered 6to4. Asserting the *reason*
    names the inner address pins the unwrap itself rather than a verdict the
    stdlib might reach by another route on one Python.
    """
    reason = _http.check_address(wrapped)
    assert reason is not None
    assert inner in reason, f"the wrapper was not unwrapped: {reason!r}"


@pytest.mark.parametrize("addr", ["224.0.0.1", "239.255.255.250", "ff02::1"])
def test_multicast_is_named_as_multicast(addr: str) -> None:
    """Multicast is already non-global, so the branch changes only the sentence.

    That is the point of it: "not a public address (loopback, link-local,
    private…)" sends a reader looking for a private range that is not there.
    The branch exists for the message, so the message is what is asserted.
    """
    reason = _http.check_address(addr)
    assert reason is not None
    assert "multicast" in reason


def test_an_unparseable_address_fails_closed() -> None:
    """No current caller reaches this — both go through `ip_address` or
    `getaddrinfo` first. It is the direction the function falls when a future
    one does not, and a guard's default is worth a test even when nothing
    exercises it yet."""
    assert _http.check_address("not-an-ip") is not None
    assert _http.check_address("") is not None


@pytest.mark.parametrize("addr", ["93.184.216.34", "140.82.121.4", "2606:50c0:8000::153"])
def test_public_addresses_are_permitted(addr: str) -> None:
    assert _http.check_address(addr) is None, f"{addr} is public and was refused"


def test_the_refusal_is_not_an_oserror() -> None:
    """Same reasoning as `RedirectRefused` and `ResponseTooLarge` (#761, #766).

    `_download_images` caught `(urllib.error.URLError, OSError)` and `continue`d.
    If the new refusal were an `OSError`, every hostile URL would be swallowed by
    that same handler and the op would print "0 downloaded" with no reason — the
    silent skip this issue is partly about.
    """
    assert not issubclass(_http.DestinationRefused, OSError)


# ---------------------------------------------------------------------------
# 2. The target policy — scheme and host, before any DNS or connect
# ---------------------------------------------------------------------------

# Read lazily: on unfixed `master` neither name exists, and a module-level
# lookup turns every test in this file into one collection error, which hides
# the RED count this fix is measured by.
def _gh_hosts():
    return getattr(issue, "IMAGE_HOSTS", ())





@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/role",
        "http://127.0.0.1:8080/admin",
        "http://[::1]/x.png",
        "http://10.0.0.5/x.png",
        "https://attacker.example/x.png",
        "https://evilgithubusercontent.com/x.png",
        "https://user-images.githubusercontent.com.evil.test/x.png",
        "https://notgithub.com/user-attachments/assets/1",
        "file:///etc/passwd",
        "ftp://internal/x",
        "gopher://127.0.0.1:11211/_stat",
        "https:///x.png",
    ],
)
def test_off_allowlist_targets_are_refused(url: str) -> None:
    reason = _http.check_fetch_target(url, _gh_hosts())
    assert reason is not None, f"{url} was permitted"


@pytest.mark.parametrize(
    "url",
    [
        "https://user-images.githubusercontent.com/1/x.png",
        "https://private-user-images.githubusercontent.com/1/x.png?jwt=a.b.c",
        "https://raw.githubusercontent.com/o/r/main/x.png",
        "https://github.com/user-attachments/assets/0000-1111",
        "https://GitHub.com/user-attachments/assets/0000-1111",
    ],
)
def test_github_asset_hosts_are_permitted(url: str) -> None:
    assert _http.check_fetch_target(url, _gh_hosts()) is None, f"{url} is a real GitHub asset URL and was refused"


@pytest.mark.parametrize("url", ["ftp://github.com/x.png", "file://github.com/x.png"])
def test_a_bad_scheme_on_an_allowlisted_host_is_refused_for_the_scheme(url: str) -> None:
    """Every scheme fixture above also fails the host allowlist, so the scheme
    check was untested until this one: an allowlisted host reached over a scheme
    that is not http(s). `ftp://` is a live SSRF primitive."""
    reason = _http.check_fetch_target(url, _gh_hosts())
    assert reason is not None
    assert "scheme" in reason, f"refused, but not for the scheme: {reason!r}"


def test_a_hostless_url_is_refused_for_being_hostless() -> None:
    reason = _http.check_fetch_target("https:///x.png", _gh_hosts())
    assert reason is not None
    assert "no host" in reason, f"refused, but not for the missing host: {reason!r}"


def test_an_allowlisted_ip_literal_is_still_address_checked() -> None:
    """The allowlist and the address policy are independent layers.

    Every other test has the allowlist doing the work, which hides whether the
    address check inside `check_fetch_target` runs at all. Allowlist the loopback
    literal — as `SUPERTOOL_IMAGE_HOSTS` would let an operator do by accident —
    and the address policy must still refuse it.
    """
    assert _http.check_fetch_target("http://127.0.0.1/x.png", ("127.0.0.1",)) is not None
    assert _http.check_fetch_target("http://169.254.169.254/x", ("169.254.169.254",)) is not None
    # ...and the opt-out is what turns it off, nothing else.
    assert _http.check_fetch_target(
        "http://127.0.0.1/x.png", ("127.0.0.1",), allow_private=True
    ) is None


def test_the_allowlist_suffix_match_requires_the_dot() -> None:
    """`endswith("githubusercontent.com")` also accepts `evilgithubusercontent.com`.

    The same defect CodeQL flagged in `_owner_repo` as
    py/incomplete-url-substring-sanitization, one function away.
    """
    assert _http.check_fetch_target("https://evilgithubusercontent.com/x.png", _gh_hosts()) is not None


# ---------------------------------------------------------------------------
# Loopback servers
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    body = b"\x89PNG\r\n\x1a\n" + b"p" * 64
    content_type = "image/png"
    redirect_to: str | None = None
    hits: list = []

    def do_GET(self) -> None:  # noqa: N802
        type(self).hits.append(self.path)
        if self.redirect_to:
            self.send_response(302)
            self.send_header("Location", self.redirect_to)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", self.content_type)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *a: object) -> None:
        pass


def _serve(cls: type) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _stop(srv: ThreadingHTTPServer) -> None:
    """Stop a test server so that its port is dead.

    Both calls, in this order, and neither is optional. `shutdown()` ends the
    `serve_forever` loop; `server_close()` closes the listening socket. Only
    the second one makes the port refuse — without it the socket stays bound
    and the kernel keeps completing handshakes into a backlog nothing accepts
    from, so a client connects and then blocks waiting for a reply that will
    never be written.
    """
    srv.shutdown()
    srv.server_close()


def test_a_stopped_test_server_leaves_nothing_listening() -> None:
    """`_stop` has to make the port refuse, not merely stop answering.

    `shutdown()` ends the `serve_forever` loop and nothing else: the listening
    socket stays bound, so the kernel goes on completing handshakes into a
    backlog no one accepts from. A client connects — successfully — and then
    waits for a reply that is never coming, for the whole of its request
    timeout where it has one and forever where it does not. That is the shape
    of a suite that stalls near the end and never names a test, so the property
    is asserted here rather than assumed in a comment at each call site.
    """
    srv = _serve(_Handler)
    port = srv.server_port
    _stop(srv)
    with socket.socket() as sock:
        sock.settimeout(5)
        with pytest.raises(ConnectionRefusedError):
            sock.connect(("127.0.0.1", port))


@pytest.fixture
def origin():
    class H(_Handler):
        hits: list = []
    srv = _serve(H)
    H.base = f"http://127.0.0.1:{srv.server_port}"  # type: ignore[attr-defined]
    yield H
    _stop(srv)


# Loopback is exactly what the policy refuses, so the transport tests opt out of
# the address half explicitly. That opt-out is the reason the *first* section
# above tests the address policy directly rather than through a server.
LOOPBACK = ("127.0.0.1",)


# ---------------------------------------------------------------------------
# 3. `_http.download` — transport
# ---------------------------------------------------------------------------

def test_download_writes_only_after_the_whole_body_passed(origin, tmp_path) -> None:
    dest = tmp_path / "ok.png"
    n = _http.download(
        origin.base + "/x.png", str(dest),
        allowed_hosts=LOOPBACK, allow_private=True, limit=1 << 20, timeout=5,
    )
    assert dest.read_bytes() == _Handler.body
    assert n == len(_Handler.body)


def test_download_refuses_an_oversized_body_and_writes_nothing(origin, tmp_path) -> None:
    dest = tmp_path / "big.png"
    with pytest.raises(_http.ResponseTooLarge):
        _http.download(
            origin.base + "/x.png", str(dest),
            allowed_hosts=LOOPBACK, allow_private=True, limit=8, timeout=5,
        )
    assert not dest.exists(), "a refused body reached the disk"


def test_download_refuses_a_non_image_content_type(origin, tmp_path) -> None:
    """The metadata service answers `text/plain`. An image fetch that saves it
    is handing the agent a document to read, which is the severity in #817."""
    class H(_Handler):
        hits: list = []
        content_type = "text/plain"
        body = b"SUPER-SECRET-IAM-CREDENTIALS"
    srv = _serve(H)
    dest = tmp_path / "x.png"
    try:
        with pytest.raises(_http.DestinationRefused) as exc:
            _http.download(
                f"http://127.0.0.1:{srv.server_port}/x.png", str(dest),
                allowed_hosts=LOOPBACK, allow_private=True, limit=1 << 20,
                timeout=5, content_types=("image/",),
            )
    finally:
        _stop(srv)
    assert "text/plain" in str(exc.value)
    assert not dest.exists()
    assert b"SECRET" not in b"".join(p.read_bytes() for p in tmp_path.iterdir())


def test_download_refuses_a_redirect_off_the_allowlist(origin, tmp_path) -> None:
    """The hole the audit named: `urlretrieve` followed this; `_http` refuses it."""
    target = _serve(_Handler)
    class R(_Handler):
        hits: list = []
        redirect_to = f"http://127.0.0.1:{target.server_port}/leak.png"
    rsrv = _serve(R)
    dest = tmp_path / "x.png"
    try:
        with pytest.raises((_http.DestinationRefused, _http.RedirectRefused)):
            _http.download(
                f"http://127.0.0.1:{rsrv.server_port}/x.png", str(dest),
                allowed_hosts=("example.invalid",), allow_private=True,
                limit=1 << 20, timeout=5,
            )
    finally:
        _stop(rsrv)
        _stop(target)
    assert not dest.exists()
    assert _Handler.hits == [], "the redirect destination was contacted"


def test_a_redirect_off_the_allowlist_is_refused_at_the_hop(tmp_path) -> None:
    """Hop one allowlisted, hop two not — the only shape that exercises the
    handler.

    The other redirect test refuses at hop one, so `DestinationRedirectHandler`
    never runs in it. Here `127.0.0.1` is allowlisted and the 302 points at
    `localhost`, a different name for the same machine and not on the list.
    """
    target = _serve(_Handler)
    _Handler.hits.clear()

    class R(_Handler):
        hits: list = []
        redirect_to = f"http://localhost:{target.server_port}/leak.png"
    rsrv = _serve(R)
    dest = tmp_path / "x.png"
    try:
        with pytest.raises(_http.DestinationRefused) as exc:
            _http.download(
                f"http://127.0.0.1:{rsrv.server_port}/x.png", str(dest),
                allowed_hosts=("127.0.0.1",), allow_private=True,
                limit=1 << 20, timeout=5,
            )
    finally:
        _stop(rsrv)
        _stop(target)
    assert "localhost" in str(exc.value)
    assert R.hits == ["/x.png"], "the first hop should have happened"
    assert _Handler.hits == [], "the redirect destination was contacted"
    assert not dest.exists()


def test_a_name_that_resolves_privately_is_refused_before_connecting(tmp_path, monkeypatch) -> None:
    """The allowlist bounds the *name*; this bounds the address behind it.

    An allowlisted name whose DNS answer is a private address is either a
    poisoned resolver or an internal split-horizon entry, and either way the
    fetch must not happen. Verified by replacing the resolver, because making a
    real githubusercontent.com name answer 169.254.169.254 is not something a
    test may do.
    """
    monkeypatch.setattr(
        _http.socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("169.254.169.254", 80))],
    )
    dest = tmp_path / "x.png"
    with pytest.raises(_http.DestinationRefused) as exc:
        _http.download(
            "https://user-images.githubusercontent.com/1/x.png", str(dest),
            allowed_hosts=_gh_hosts(), limit=1 << 20, timeout=5,
        )
    assert "resolves to" in str(exc.value)
    assert not dest.exists()


def test_one_public_answer_does_not_excuse_a_private_one(tmp_path, monkeypatch) -> None:
    """Any, not all. urllib takes whichever address the resolver hands it first,
    so a name answering with both is refused on the strength of the bad one —
    accepting it because a good one was also offered is the rebinding attack
    with the attacker doing less work."""
    monkeypatch.setattr(
        _http.socket, "getaddrinfo",
        lambda *a, **k: [
            (2, 1, 6, "", ("140.82.121.4", 443)),
            (2, 1, 6, "", ("127.0.0.1", 443)),
        ],
    )
    with pytest.raises(_http.DestinationRefused):
        _http.download(
            "https://user-images.githubusercontent.com/1/x.png",
            str(tmp_path / "x.png"),
            allowed_hosts=_gh_hosts(), limit=1 << 20, timeout=5,
        )


def test_download_refuses_the_target_before_it_connects(tmp_path) -> None:
    """A refused target must cost zero packets — the SYN to the metadata service
    is itself the probe, and a request that reaches it has already told the
    attacker the machine is reachable."""
    srv = _serve(_Handler)
    _Handler.hits.clear()
    dest = tmp_path / "x.png"
    try:
        with pytest.raises(_http.DestinationRefused):
            _http.download(
                f"http://127.0.0.1:{srv.server_port}/x.png", str(dest),
                allowed_hosts=_gh_hosts(), limit=1 << 20, timeout=5,
            )
    finally:
        _stop(srv)
    assert _Handler.hits == [], "the refused target was contacted anyway"
    assert not dest.exists()


# ---------------------------------------------------------------------------
# 4. The op — the reported attack, end to end
# ---------------------------------------------------------------------------

ATTACK_BODIES = {
    "link-local metadata": "![x](http://169.254.169.254/latest/meta-data/iam/security-credentials/role)",
    "loopback": "![x](http://127.0.0.1:8080/admin/keys)",
    "loopback v6": "![x](http://[::1]:9200/_cat/indices)",
    "rfc1918": "![x](http://192.168.1.1/cgi-bin/config)",
    "cgnat": "![x](http://100.64.0.1/x.png)",
    "off-allowlist public": "![x](https://attacker.example/collect.png)",
    "lookalike host": "![x](https://evilgithubusercontent.com/x.png)",
    "file scheme in the alt": "![x](http://169.254.169.254:80/latest/api/token)",
}


@pytest.mark.parametrize("name,body", list(ATTACK_BODIES.items()), ids=list(ATTACK_BODIES))
def test_the_reported_attack_fetches_nothing(name: str, body: str, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(issue, "IMAGE_DIR", str(tmp_path / "img"))
    urls = issue._extract_image_urls(body)
    assert urls, "the fixture is not exercising the extractor"
    results = issue._download_images(urls, "817")
    assert [r.state for r in results] == [issue.IMAGE_REFUSED], f"{name} was not refused: {results}"
    on_disk = [p for p in (tmp_path / "img").rglob("*") if p.is_file()]
    assert on_disk == [], f"{name} left bytes on disk: {on_disk}"
    assert not (tmp_path / "img").exists(), (
        f"{name} was refused but still created {tmp_path / 'img'} — a refusal "
        f"should leave nothing, not an empty directory"
    )


def test_a_redirect_toward_the_metadata_service_is_refused(tmp_path, monkeypatch) -> None:
    """The allowlist is checked on every hop, not only the first.

    Refused before any connection here, because the *first* hop is already off
    the allowlist — which is the point: an attacker cannot reach the redirect
    stage without a GitHub-hosted URL to start from.
    """
    class R(_Handler):
        hits: list = []
        redirect_to = "http://169.254.169.254/latest/meta-data/"
    srv = _serve(R)
    monkeypatch.setattr(issue, "IMAGE_DIR", str(tmp_path / "img"))
    try:
        results = issue._download_images([f"http://127.0.0.1:{srv.server_port}/x.png"], "817")
    finally:
        _stop(srv)
    assert [r.state for r in results] == [issue.IMAGE_REFUSED]


# ---------------------------------------------------------------------------
# 5. Refusal is loud — three states, never silence (#780, docs/validators.md)
# ---------------------------------------------------------------------------

def test_a_refused_image_is_disclosed_with_its_reason(capsys, tmp_path, monkeypatch) -> None:
    """A skipped image that prints nothing reads as "the issue had no images".

    That reading has an action attached — the reader stops looking for the
    screenshot the reporter says they attached — and it is this repo's
    most-filed defect class. Landing it inside a security control would mean the
    control's own refusals are the thing nobody sees.
    """
    monkeypatch.setattr(issue, "IMAGE_DIR", str(tmp_path / "img"))
    url = "http://169.254.169.254/latest/meta-data/"
    issue._print_images(issue._download_images([url], "817"))
    out = capsys.readouterr().out
    assert "169.254.169.254" in out, f"the refused URL is not named: {out!r}"
    assert "refused" in out.lower(), f"the refusal is not stated: {out!r}"
    assert "0 downloaded" not in out or "1 refused" in out, "a refusal was counted as a plain non-download"
    assert "no images" not in out.lower()


def test_the_heading_distinguishes_all_three_states(capsys, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(issue, "IMAGE_DIR", str(tmp_path / "img"))
    results = [
        issue.ImageResult("https://user-images.githubusercontent.com/a.png", issue.IMAGE_FETCHED, "/tmp/a.png", None),
        issue.ImageResult("http://169.254.169.254/x", issue.IMAGE_REFUSED, None, "not a public address"),
        issue.ImageResult("https://user-images.githubusercontent.com/b.png", issue.IMAGE_UNKNOWN, None, "timed out"),
    ]
    issue._print_images(results)
    out = capsys.readouterr().out
    assert "1 fetched" in out and "1 refused" in out
    assert "1 could not" in out.lower() or "1 unknown" in out.lower()
    assert "/tmp/a.png" in out
    assert "timed out" in out


def test_a_network_failure_is_not_reported_as_a_refusal(capsys, tmp_path, monkeypatch) -> None:
    """"We declined to fetch this" and "we tried and could not tell" are
    different facts with different next actions. Collapsing them makes the
    security control look like it fired when the network merely blinked."""
    monkeypatch.setattr(issue, "IMAGE_DIR", str(tmp_path / "img"))
    srv = _serve(_Handler)
    port = srv.server_port
    _stop(srv)  # the port is dead now, and the test above proves it
    monkeypatch.setattr(issue, "IMAGE_HOSTS", ("127.0.0.1",))
    monkeypatch.setattr(issue, "IMAGE_ALLOW_PRIVATE", True)
    results = issue._download_images([f"http://127.0.0.1:{port}/x.png"], "817")
    assert [r.state for r in results] == [issue.IMAGE_UNKNOWN], results
    issue._print_images(results)
    out = capsys.readouterr().out.lower()
    assert "refused" not in out.split("could not")[0] or "could not" in out


def test_the_untrusted_nature_of_a_fetched_image_is_stated(capsys) -> None:
    """Judgment call 3, kept rather than dropped — so the ground has to be said.

    The bytes are attacker-chosen, uploaded to a GitHub asset host. That is the
    same trust class as the issue body, which this op already prints behind
    `_untrusted.fence()`. Printing the path is therefore no *new* trust
    boundary — but only while the reader knows it, so the section says so.
    """
    issue._print_images([
        issue.ImageResult("https://user-images.githubusercontent.com/a.png", issue.IMAGE_FETCHED, "/tmp/a.png", None),
    ])
    out = capsys.readouterr().out.lower()
    assert "untrusted" in out or "not trusted" in out


# ---------------------------------------------------------------------------
# 6. The filename came from attacker text too
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name",
    ["..", "../../../../etc/cron.d/x", "..%2f..%2fevil", "", "." * 200, "a\nb", "a;rm -rf b"],
)
def test_the_saved_filename_cannot_leave_the_issue_directory(name: str, tmp_path) -> None:
    out_dir = tmp_path / "gh" / "817"
    out_dir.mkdir(parents=True)
    path = Path(issue._local_path(str(out_dir), f"https://user-images.githubusercontent.com/{name}", 0))
    assert out_dir.resolve() == path.resolve().parent, f"{name!r} escaped to {path}"
    # Containment via the index prefix alone would pass the line above while
    # leaving `00_..%2f..%2fevil` on disk — a name that is safe by accident, and
    # only until somebody changes the prefix. The shape of the name is the
    # actual claim, so it is what is asserted.
    assert re.fullmatch(r"[0-9]{2}_[A-Za-z0-9._-]+", path.name), f"unsafe name {path.name!r}"
    assert ".." not in path.name, f"traversal survived scrubbing: {path.name!r}"
    assert "%" not in path.name, f"an encoded separator survived: {path.name!r}"


def test_the_private_address_opt_out_is_off_in_shipped_code() -> None:
    """The layer above (the allowlist) refuses loopback URLs whatever this is
    set to, which is exactly why flipping it goes unnoticed. Two layers are only
    two layers while both are on."""
    assert issue.IMAGE_ALLOW_PRIVATE is False


def test_two_urls_cannot_collide_onto_one_file(tmp_path) -> None:
    a = issue._local_path(str(tmp_path), "https://user-images.githubusercontent.com/1/x.png", 0)
    b = issue._local_path(str(tmp_path), "https://user-images.githubusercontent.com/2/x.png", 1)
    assert a != b, "the second image overwrote the first"


# ---------------------------------------------------------------------------
# 7. Structural — the scan that would have caught this in the first place
# ---------------------------------------------------------------------------

def test_no_urlretrieve_call_sites_remain_under_presets() -> None:
    """`tests/test_security_redirect.py` scans for `urllib.request.urlopen(` and
    `tests/test_http_bounds.py` for an argument-less `.read()`. Neither sees
    `urlretrieve`, which is a third door into the same default opener — and it
    is the one #817 came through, in a file rewritten in the same window that
    added the guard it walked around."""
    offenders = []
    for path in sorted((ROOT / "presets").rglob("*.py")):
        for num, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "urlretrieve(" in line and not line.lstrip().startswith("#"):
                offenders.append(f"{path.relative_to(ROOT)}:{num}")
    assert offenders == [], (
        "urlretrieve uses the default opener: no redirect guard, no size cap, no "
        f"deadline, no destination policy. Use _http.download(): {offenders}"
    )


def test_the_download_opener_is_not_the_default_one() -> None:
    handlers = [type(h).__name__ for h in _http.download_opener(("x",), False).handlers]
    assert "HTTPRedirectHandler" not in handlers
    assert any("Redirect" in h for h in handlers)


def test_the_policy_documents_what_it_does_not_cover() -> None:
    """A half-guard described as a guard is worse than none. DNS rebinding and
    an HTTP proxy both defeat the address half, and both are stated in the
    module rather than left for the next reader to discover."""
    doc = _http.download.__doc__ or ""
    assert "rebind" in doc.lower(), "the TOCTOU gap between resolve and connect is not stated"
    assert "proxy" in doc.lower(), "the proxy bypass of the address check is not stated"
