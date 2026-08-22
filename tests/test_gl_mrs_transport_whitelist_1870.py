"""gl_mrs has no transport-marker whitelist and no rate-limit arm (#1870).

`gh_prs` classifies a transport failure off `gh`'s exit 4 plus a marker
whitelist added in #1568: DNS, a refused connection, a reset socket and the
rest of Go's `net`/`net/http` vocabulary all raise `RadarUnreachable`, so a
caller told "unreachable" knows to retry. `gl_mrs` classified on structure
only -- a spawn that never completed, a negative returncode, `_auth_probe`'s
answer -- and anything else, transport failures included, fell into the
widest class, `RadarError`, which a caller reads as "the board says X, stop".

This is the acceptance test the issue itself names: raise each tier against a
transport failure and assert both produce `RadarUnreachable`. Every "must be
unreachable" case is paired with a "must NOT be unreachable" case driven
through the same fake, for the reason `test_radar_error_classes_1847.py`
already states -- an assertion that a class is *absent* passes on a tier that
raises nothing at all.

**Reasoned, not observed.** `glab` is not authenticated on the machine this
was written on (`glab auth login` was never run here), so no live GitLab
transport failure was captured. Every stderr string below is either the exact
one #1870 names as reachable input (`dial tcp: lookup gitlab.com: no such
host`, from `glab mr list` exiting 1) or ported from `gh_prs`'s own whitelist
on the reasoning that `glab`, like `gh`, is a Go binary whose transport layer
surfaces `net`/`net/http` stdlib errors verbatim -- a claim this file states
as reasoned rather than measured against a real `glab` binary. Nothing here
claims a specific `glab` version produced any of these strings.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
WATCH_DIR = ROOT / "presets" / "watch"


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gl_tier = _module("radar_transport_1870_gl", WATCH_DIR / "tiers" / "gl_mrs.py")


class _Result:
    def __init__(self, out: str = "", err: str = "", code: int = 0):
        self.stdout, self.stderr, self.returncode = out, err, code


def _fake_glab(monkeypatch, *, out: str | None = None, err: str = "",
               code: int = 0, raises: BaseException | None = None):
    def _run(*_a, **_k):
        if raises is not None:
            raise raises
        body = json.dumps([]) if out is None else out
        return _Result(body, err, code)
    monkeypatch.setattr(gl_tier.mrs, "_run", _run)


def _gl_raises(monkeypatch, **kw) -> BaseException:
    _fake_glab(monkeypatch, **kw)
    with pytest.raises(gl_tier.RadarError) as caught:
        gl_tier._query({}, 20)
    return caught.value


# ---------------------------------------------------------------------------
# positive: transport failures now raise RadarUnreachable
# ---------------------------------------------------------------------------

def test_the_issues_own_named_input_is_unreachable(monkeypatch) -> None:
    """`glab mr list` exiting 1 with a DNS failure -- the exact reachable
    input #1870 names."""
    exc = _gl_raises(monkeypatch, code=1,
                     err="dial tcp: lookup gitlab.com: no such host")
    assert isinstance(exc, gl_tier.RadarUnreachable)


def test_a_refused_connection_is_unreachable(monkeypatch) -> None:
    exc = _gl_raises(monkeypatch, code=1,
                     err='Get "https://gitlab.com/api/v4/projects": '
                         'dial tcp 1.2.3.4:443: connect: connection refused')
    assert isinstance(exc, gl_tier.RadarUnreachable)


def test_a_reset_socket_is_unreachable(monkeypatch) -> None:
    exc = _gl_raises(monkeypatch, code=1, err="read: connection reset by peer")
    assert isinstance(exc, gl_tier.RadarUnreachable)


def test_a_tls_handshake_timeout_is_unreachable(monkeypatch) -> None:
    exc = _gl_raises(monkeypatch, code=1,
                     err="net/http: TLS handshake timeout")
    assert isinstance(exc, gl_tier.RadarUnreachable)


def test_a_rate_limited_response_is_unreachable(monkeypatch) -> None:
    """GitLab's own rate-limit status, 429 -- not `gh`'s 403, because the two
    forges use different codes for the same class of refusal."""
    exc = _gl_raises(monkeypatch, code=1,
                     err="GET https://gitlab.com/api/v4/merge_requests: "
                         "429 Too Many Requests")
    assert isinstance(exc, gl_tier.RadarUnreachable)


# ---------------------------------------------------------------------------
# negative controls: an unrecognised failure, and one that arrived and was
# wrong, both stay a plain RadarError. Without these, the positive cases above
# would pass on a tier that raises RadarUnreachable unconditionally.
# ---------------------------------------------------------------------------

def test_a_product_failure_nothing_explains_is_still_not_unreachable(
        monkeypatch) -> None:
    exc = _gl_raises(monkeypatch, code=1,
                     err="the project 'acme/widget' could not be found")
    assert isinstance(exc, gl_tier.RadarError)
    assert not isinstance(exc, gl_tier.RadarUnreachable)


def test_an_unknown_option_stays_not_unreachable(monkeypatch) -> None:
    exc = _gl_raises(monkeypatch, code=1, err="unknown option --milestne")
    assert isinstance(exc, gl_tier.RadarError)
    assert not isinstance(exc, gl_tier.RadarUnreachable)


# ---------------------------------------------------------------------------
# the caller's own question, asked the way #1870's own acceptance test asks
# it: sort two failures of different kinds by `except` alone, no message read.
# ---------------------------------------------------------------------------

def test_a_caller_sorts_transport_from_product_failure_without_reading_a_message(
        monkeypatch) -> None:
    def _classify(**kw) -> str:
        _fake_glab(monkeypatch, **kw)
        try:
            gl_tier._query({}, 20)
        except gl_tier.RadarUnreachable:
            return "retry"
        except gl_tier.RadarError:
            return "verdict"
        return "no failure at all"

    assert _classify(code=1, err="dial tcp: lookup gitlab.com: "
                                  "no such host") == "retry"
    assert _classify(code=1, err="unknown option --milestne") == "verdict"
