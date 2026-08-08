"""1035 — `gl-api` must refuse anything that names its own host.

`path_refusal()` shipped with four refusals — empty, flag-shaped, whitespace,
`graphql` — and the value was then handed to `glab api` verbatim. `glab api`
accepts an absolute URL as its endpoint and attaches the instance's
`Private-Token` header to it, so `gl-api:http://127.0.0.1:8765/leak` sent a
live GitLab credential to a host the caller's *input* chose, and rendered the
listener's reply as an ordinary result with `PASS` on it.

The input is not hypothetically remote: a path can arrive out of an issue body,
an MR description or a CI job name, all of which this repo already treats as
text a stranger wrote.

So the rule pinned here is **a path may not name a host**, expressed as an
allowlist of what a GitLab API path may contain rather than a denylist of
schemes:

* nothing scheme-shaped at the front (`http:`, `HtTpS://`, `file:`, `ftp:`),
  in any casing and with or without the `//`;
* no `//` inside the path portion, which is what a protocol-relative
  `//evil.host/x` and a userinfo `//gitlab.com@evil.host/x` both need;
* no literal backslash, which some URL parsers fold into `/`;
* only characters a real API path or query can contain — no control bytes, no
  `#`, no `<`/`>`;
* and the authority checks again on the percent-decoded form, so
  `http%3A%2F%2Fevil.host` and `%2F%2Fevil.host` cannot smuggle past the first
  pass. On that pass a backslash is folded into `/` and checked rather than
  refused outright, so `%5C%5Cevil.host` is still caught and a filename
  spelled `src%5Cmain.rs` is not (#1043, pinned in its own file).

Refused, never sanitised: stripping the scheme would send *a* request, and the
caller would read the answer as the one they asked for.

The other half of the file is the breakage budget. A rule this shape is only
worth having if `projects/:id/members/all`, a percent-encoded group path, a
query string, an npm scoped package and an ISO timestamp all still go through.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

PRESET_PATH = Path(__file__).parent.parent / "presets" / "gitlab" / "api.py"
_spec = importlib.util.spec_from_file_location("gitlab_api_1035", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api)


class _Result:
    def __init__(self, stdout: str = "{}", stderr: str = "",
                 returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _argv_glab_saw(monkeypatch: Any, argjoin: str) -> list[str]:
    """Drive main() with one `{argjoin}` argument; return the argv glab saw."""
    seen: list[str] = []

    def _fake(cmd, *a, **k):  # noqa: ANN001
        seen.extend(cmd)
        return _Result()

    monkeypatch.setattr(api.subprocess, "run", _fake)
    monkeypatch.setattr(sys, "argv", ["api.py", argjoin])
    return seen


#: Every one of these, given to `gl-api`, used to reach `glab api` unchanged.
#: The comment on each is the host the token would have gone to.
NAMES_A_HOST = [
    "http://127.0.0.1:8765/leak",             # the captured reproduction
    "https://evil.example/x",                 # plain https
    "HtTpS://evil.example/x",                 # scheme casing is not lowercase
    "HTTP://EVIL.EXAMPLE/X",
    "http:evil.example/x",                    # scheme with no authority slashes
    "ftp://evil.example/x",                   # any scheme, not just http
    "file:///etc/passwd",                     # not even a network scheme
    "//evil.example/x",                       # protocol-relative
    "///evil.example/x",                      # extra slash, same trick
    "https://gitlab.com@evil.example/x",      # userinfo relocates the host
    "//gitlab.com@evil.example/x",            # userinfo without a scheme
    "http://evil.example/x?ref=main",         # a query does not make it a path
    "\\\\evil.example\\x",                    # backslashes fold to / in places
    "/\\evil.example/x",
    "http%3A%2F%2Fevil.example/x",            # percent-encoded scheme
    "%2F%2Fevil.example/x",                   # percent-encoded //
    "%5C%5Cevil.example/x",                   # percent-encoded backslashes fold to //
    "projects/1/issues#https://evil.example", # a fragment is not path syntax
    "projects/1/issues\x00",                  # NUL is not whitespace
    "projects/1/\x1b[31missues",              # nor is an escape sequence
]


@pytest.mark.parametrize("path", NAMES_A_HOST)
def test_a_path_that_names_a_host_is_refused(path: str) -> None:
    refusal = api.path_refusal(path)
    assert refusal, f"{path!r} was accepted as an API path"
    assert refusal.startswith("ERROR")


@pytest.mark.parametrize("path", NAMES_A_HOST)
def test_a_path_that_names_a_host_never_reaches_glab(path: str,
                                                     monkeypatch: Any,
                                                     capsys: Any) -> None:
    """The refusal is the whole point only if no request is sent."""
    seen = _argv_glab_saw(monkeypatch, path)
    assert api.main() == 1
    assert seen == [], f"glab was invoked with {seen!r}"
    out = capsys.readouterr().out
    assert "ERROR" in out


def test_the_refusal_names_the_value_and_the_reason(capsys: Any) -> None:
    """In the shape the other four refusals use: what, and why."""
    refusal = api.path_refusal("http://127.0.0.1:8765/leak")
    assert "http://127.0.0.1:8765/leak" in refusal
    assert "token" in refusal.lower()
    assert "path" in refusal.lower()


def test_the_url_is_refused_rather_than_stripped(monkeypatch: Any,
                                                 capsys: Any) -> None:
    """No sanitised leftover ever reaches glab.

    Turning `http://evil.example/leak` into `leak` would send *a* request, and
    the caller would read the answer as the one they asked for.
    """
    seen = _argv_glab_saw(monkeypatch, "http://evil.example/leak")
    assert api.main() == 1
    assert seen == []
    capsys.readouterr()


#: The breakage budget: real paths this repo and the GitLab docs actually use.
REAL_PATHS = [
    "projects/:id/members/all",
    "projects/:id/members/all?per_page=100",
    "/projects/1/issues",                       # a leading slash is fine
    "projects/group%2Fsubgroup%2Fproject/issues",
    "projects/1/repository/files/src%2Fmain.rs/raw?ref=main",
    "projects/1/repository/files/src%5Cmain.rs/raw?ref=main",   # #1043
    "projects/1/issues?not[labels]=bug&scope=all",
    "users/1/events?after=2026-08-01&sort=desc",
    "projects/1/issues?updated_after=2026-08-01T00:00:00Z",
    "projects/:id/packages/npm/@scope/package-name",
    "groups/1/members?query=first.last%40example.com",
    "projects/1/merge_requests?labels=a,b&state=opened",
    "projects/:id/protected_branches",
    "version",
]


@pytest.mark.parametrize("path", REAL_PATHS)
def test_a_real_api_path_is_still_accepted(path: str) -> None:
    assert api.path_refusal(path) == "", f"{path!r} was refused"


@pytest.mark.parametrize("path", REAL_PATHS)
def test_a_real_api_path_still_reaches_glab_unchanged(path: str,
                                                      monkeypatch: Any,
                                                      capsys: Any) -> None:
    seen = _argv_glab_saw(monkeypatch, path)
    assert api.main() == 0
    assert path in seen
    capsys.readouterr()


def test_the_four_original_refusals_still_fire() -> None:
    """The new rule is added to them, not substituted for them."""
    assert api.path_refusal("")
    assert api.path_refusal("-X POST projects/1/star")
    assert api.path_refusal("projects/1 --method POST")
    assert api.path_refusal("graphql")
