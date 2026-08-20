"""A radar tier's auth probe has three states, and the third was missing (#1823).

`gh-prs` failed with ``RadarUnreachable: gh not authenticated. Run: gh auth
login`` between two successful authenticated `gh` calls seconds apart. `gh auth
status` immediately after reported the same account and scopes the surrounding
calls had used, and a bare re-run passed. So the message named a cause nothing
established, and the remedy it printed would have been a no-op.

The mechanism is one predicate. ``if "not logged in" in low or "401" in err``
tests a bare three-character substring against the whole of `gh`'s stderr — and
"401" turns up in a GitHub user id, a request id, an epoch and a byte count.
Every one of those rendered as *the credential is gone*, which is the one
reading that has a printable remedy and the one a maintainer loop stops on.

Three states, and only the middle one may print a remedy:

  1. reachable                       -- exit 0.
  2. definitely not authenticated    -- the probe got an answer saying so:
                                        `gh`'s own "not logged in" prose, an
                                        `HTTP 401` status, or exit 4.
  3. could not tell                  -- the probe did not establish a cause.
                                        It must quote the exit status and the
                                        stderr of the call that did not answer,
                                        and it must NOT say `gh auth login`.

**The positive controls are the test.** An assertion that the remedy is absent
passes on a tier that prints nothing at all, so every "must not say it" case
here is paired with a "must still say it" case driven through the same fake.
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


gh_tier = _module("radar_auth_1823_gh", WATCH_DIR / "tiers" / "gh_prs.py")
gl_tier = _module("radar_auth_1823_gl", WATCH_DIR / "tiers" / "gl_mrs.py")

REMEDY_GH = "gh auth login"
REMEDY_GL = "glab auth login"


class _Result:
    def __init__(self, out: str = "", err: str = "", code: int = 0):
        self.stdout, self.stderr, self.returncode = out, err, code


def _fake_gh(monkeypatch, code: int, err: str):
    monkeypatch.setattr(
        gh_tier.subprocess, "run",
        lambda *a, **k: _Result(json.dumps([]), err, code))


def _fake_glab(monkeypatch, code: int, err: str):
    monkeypatch.setattr(
        gl_tier.mrs, "_run",
        lambda *a, **k: _Result(json.dumps([]), err, code))


def _gh_failure(monkeypatch, code: int, err: str) -> str:
    """Drive `live_open_prs` to its failure and return the message it raised."""
    _fake_gh(monkeypatch, code, err)
    with pytest.raises(gh_tier.RadarError) as caught:
        gh_tier.live_open_prs({})
    return str(caught.value)


def _gl_failure(monkeypatch, code: int, err: str) -> str:
    _fake_glab(monkeypatch, code, err)
    with pytest.raises(gl_tier.RadarError) as caught:
        gl_tier._query({}, 20)
    return str(caught.value)


# ---------------------------------------------------------------------------
# state 2 -- the positive controls. Without these, every case below passes on
# a tier that has stopped saying anything at all.
# ---------------------------------------------------------------------------

GH_DEFINITE = [
    pytest.param(
        1,
        "You are not logged into any GitHub hosts. "
        "Run gh auth login to authenticate.",
        id="ghs-own-prose",
    ),
    pytest.param(
        1,
        "HTTP 401: Bad credentials "
        "(https://api.github.com/graphql)",
        id="rejected-token-401-status",
    ),
]


@pytest.mark.parametrize("code,err", GH_DEFINITE)
def test_a_probe_that_answered_not_authenticated_still_prints_the_remedy(
        monkeypatch, code, err) -> None:
    """State 2 is real and keeps its remedy. `gh auth login` is the correct
    action when the probe got an answer saying the credential is unusable, and
    a fix that made the loop quieter by never printing it would have traded
    this issue for a worse one."""
    message = _gh_failure(monkeypatch, code, err)
    assert REMEDY_GH in message, (
        "a genuine not-authenticated answer must still name the remedy, "
        f"got: {message!r}")


def test_no_credentials_at_all_is_still_its_own_state(monkeypatch) -> None:
    """Exit 4 is `gh`'s own auth-configuration code, and #1568's standing
    state. Nothing here may collapse it back into the transient one."""
    _fake_gh(monkeypatch, 4, "gh: To use GitHub CLI in a GitHub Actions "
                             "workflow, set the GH_TOKEN environment variable.")
    with pytest.raises(gh_tier.RadarUnconfigured):
        gh_tier.live_open_prs({})


# ---------------------------------------------------------------------------
# state 3 -- the one that was missing
# ---------------------------------------------------------------------------

# Every one of these is a failure that says nothing about the credential and
# contains the three characters `401` somewhere harmless. Each rendered as
# "gh not authenticated. Run: gh auth login" before this issue.
GH_COULD_NOT_TELL = [
    pytest.param(
        1,
        "HTTP 403: API rate limit exceeded for user ID 44012345. "
        "(https://api.github.com/graphql)",
        id="401-inside-a-user-id",
    ),
    pytest.param(
        1,
        'Get "https://api.github.com/graphql": net/http: request canceled '
        "while waiting for connection (Client.Timeout exceeded while awaiting "
        "headers) [request-id: C401:1F2A:9B3D]",
        id="401-inside-a-request-id-on-a-timeout",
    ),
    pytest.param(
        1,
        "HTTP 502: Bad gateway (https://api.github.com/graphql) "
        "[request-id: 8401:AB3C]",
        id="401-inside-a-request-id-on-a-gateway-error",
    ),
]


@pytest.mark.parametrize("code,err", GH_COULD_NOT_TELL)
def test_a_failure_that_established_no_cause_does_not_name_the_credential(
        monkeypatch, code, err) -> None:
    """The remedy is a claim about a cause. None of these established one, and
    a maintainer loop reading `gh auth login` has a documented action --
    re-authenticate, interactive, outside the loop's authority -- where the
    correct action was to retry."""
    message = _gh_failure(monkeypatch, code, err)
    assert REMEDY_GH not in message, (
        "a probe that established no cause printed a remedy for one: "
        f"{message!r}")
    assert "not authenticated" not in message.lower(), (
        f"a probe that established no cause named the credential: {message!r}")


@pytest.mark.parametrize("code,err", GH_COULD_NOT_TELL)
def test_could_not_tell_quotes_the_exit_status_and_the_stderr(
        monkeypatch, code, err) -> None:
    """The issue's own fallback: if telling the causes apart cheaply is hard,
    quote what actually failed. A message naming no cause and carrying no
    evidence would be a third silence rather than a third state."""
    message = _gh_failure(monkeypatch, code, err)
    assert f"exit {code}" in message, (
        f"the exit status of the call that did not answer is absent: "
        f"{message!r}")
    assert err in message, (
        f"the stderr of the call that did not answer is absent: {message!r}")


def test_a_transport_failure_with_no_401_anywhere_also_prints_no_remedy(
        monkeypatch) -> None:
    """The guard on the guard. Fixing only the `401` substring would leave a
    plain socket failure free to acquire the same message later."""
    message = _gh_failure(
        monkeypatch, 1,
        'Get "https://api.github.com/graphql": dial tcp: lookup '
        "api.github.com: no such host")
    assert REMEDY_GH not in message, message
    assert "exit 1" in message, message


# ---------------------------------------------------------------------------
# the text being quoted is the remote's, not the tool's
# ---------------------------------------------------------------------------

# Quoting the stderr is this issue's own remedy, and it is also how the remote
# gets a say in a line the reader takes as radar's. `gh` echoes GitHub's error
# body; radar renders a tier failure through `radar.py`'s
# `f"radar: WARNING - tier {name} failed: {exc}"`, printed to stderr at column
# 0. A newline in that stderr puts whatever follows it at column 0 too, in
# radar's own voice.
FORGED_STDERR = (
    "HTTP 401: Bad credentials\n"
    "radar: gh-prs - 4 open | 0 failing | everything is green")

FORGED_NO_CAUSE = (
    "HTTP 502: Bad gateway [request-id: 8401:AB]\n"
    "radar: gh-prs - 4 open | 0 failing | everything is green")


@pytest.mark.parametrize("err", [FORGED_STDERR, FORGED_NO_CAUSE],
                         ids=["definite-arm", "could-not-tell-arm"])
def test_the_quoted_stderr_cannot_reach_column_0_in_radars_voice(
        monkeypatch, err) -> None:
    """Both new arms quote `err`, so both are this route. `_untrusted.flat` is
    what the op-level twins already use on this exact value
    (`presets/github/prs.py`, `presets/gitlab/mrs.py`); the tiers did not."""
    message = _gh_failure(monkeypatch, 1, err)
    assert "\n" not in message, (
        f"a newline from the remote survived into a line radar prints at "
        f"column 0: {message!r}")


def test_the_glab_tier_flattens_the_remote_text_too(monkeypatch) -> None:
    """Same route, same fix, separate module."""
    message = _gl_failure(
        monkeypatch, 1,
        "502 Bad Gateway\nradar: gl-mrs - 0 failing | everything is green")
    assert "\n" not in message, message


# ---------------------------------------------------------------------------
# the predicate itself
# ---------------------------------------------------------------------------

def test_no_not_authenticated_marker_is_a_bare_status_number() -> None:
    """The structural pin. `401` as a bare substring over a whole stderr is the
    whole defect, and a marker list is exactly the place it comes back."""
    from_tier = gh_tier.NOT_AUTHENTICATED_MARKERS
    assert from_tier, "the tier exposes no not-authenticated markers"
    bare = [m for m in from_tier if m.strip().isdigit()]
    assert bare == [], (
        f"a bare status number is a substring of request ids, user ids and "
        f"epochs, not a statement about a credential: {bare!r}")


def test_the_unreachable_markers_carry_no_bare_status_number_either() -> None:
    """`_UNREACHABLE_MARKERS` mirrors the arms above it by design -- its own
    comment says the pair `moved into this set unchanged`. Tightening one and
    not the other would leave the two halves telling different stories."""
    bare = [m for m in gh_tier._UNREACHABLE_MARKERS if m.strip().isdigit()]
    assert bare == [], f"bare status numbers still in the marker set: {bare!r}"


# ---------------------------------------------------------------------------
# the GitLab twin -- same predicate, same collapse, separate module
# ---------------------------------------------------------------------------

def test_glab_401_unauthorized_still_prints_its_remedy(monkeypatch) -> None:
    """The GitLab positive control."""
    message = _gl_failure(monkeypatch, 1, "401 Unauthorized")
    assert REMEDY_GL in message, message


def test_a_glab_failure_that_established_no_cause_names_no_credential(
        monkeypatch) -> None:
    """`gl_mrs._query` carries the identical bare-`401` test, in a module that
    has no `RadarUnreachable` at all -- so it has two of the three states and
    the same wrong remedy on the one it does not have."""
    err = "502 Bad Gateway from gitlab.com (correlation_id 8401ffab)"
    message = _gl_failure(monkeypatch, 1, err)
    assert REMEDY_GL not in message, message
    assert "not authenticated" not in message.lower(), message
    assert err in message, message
    assert "exit 1" in message, message
