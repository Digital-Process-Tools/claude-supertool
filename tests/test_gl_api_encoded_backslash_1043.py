"""1043 — a percent-encoded backslash is a filename, not an authority.

`host_naming_reason()` ran twice: once on the raw value and once on
`urllib.parse.unquote(...)`. The backslash rule travelled with the scheme and
`//` rules on both passes, so `src%5Cmain.rs` — the *only* spelling GitLab's
files API accepts for a file committed from Windows as `src\\main.rs` — was
refused as a URL, and that file was unreachable through the op.

The narrowing is not "allow %5C". A percent-encoded backslash cannot open an
authority *by itself*, but `%5C%5Cevil.example/x` decodes to `\\\\evil.example/x`,
and a consumer that decodes before parsing folds that into `//evil.example/x` —
the exact protocol-relative shape the decoded pass exists to catch, and one the
`//` rule does not see because the decoded form has no `/` in it.

So on the decoded pass the backslash is *folded into a slash* and the authority
rules are applied to the result, instead of being refused on sight. Raw values
keep the flat rule: a literal backslash is still never in a GitLab API path.

`glab` is unauthenticated in the dev sandbox, so nothing here calls GitLab. The
subprocess is faked and the assertion is on the argv `glab` would have seen —
which is where the refusal has to bite anyway.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

PRESET_PATH = Path(__file__).parent.parent / "presets" / "gitlab" / "api.py"
_spec = importlib.util.spec_from_file_location("gitlab_api_1043", PRESET_PATH)
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
    seen: list[str] = []

    def _fake(cmd, *a, **k):  # noqa: ANN001
        seen.extend(cmd)
        return _Result()

    monkeypatch.setattr(api.subprocess, "run", _fake)
    monkeypatch.setattr(sys, "argv", ["api.py", argjoin])
    return seen


#: Legal GitLab paths whose only correct spelling contains %5C.
ENCODED_BACKSLASH_IS_A_NAME = [
    "projects/1/repository/files/src%5Cmain.rs/raw?ref=main",
    "projects/1/repository/files/src%5cmain.rs/raw?ref=main",  # hex casing
    "projects/1/repository/files/a%5Cb%5Cc.txt/raw?ref=main",
    "projects/group%2Fsub/repository/files/win%5Cpath.rs/raw?ref=main",
]


@pytest.mark.parametrize("path", ENCODED_BACKSLASH_IS_A_NAME)
def test_an_encoded_backslash_in_a_filename_is_accepted(path: str) -> None:
    assert api.path_refusal(path) == "", f"{path!r} was refused"


@pytest.mark.parametrize("path", ENCODED_BACKSLASH_IS_A_NAME)
def test_an_encoded_backslash_path_reaches_glab_unchanged(path: str,
                                                          monkeypatch: Any,
                                                          capsys: Any) -> None:
    seen = _argv_glab_saw(monkeypatch, path)
    assert api.main() == 0
    assert path in seen
    capsys.readouterr()


#: The narrowing may not re-open any of these. Each decodes to something a
#: parser that folds `\\` into `/` reads as an authority.
STILL_NAMES_A_HOST = [
    "%5C%5Cevil.example/x",        # -> \\evil.example/x -> //evil.example/x
    "%5c%5cevil.example/x",        # hex casing
    "/%5Cevil.example/x",          # -> /\evil.example/x -> //evil.example/x
    "%5C/evil.example/x",          # -> \/evil.example/x -> //evil.example/x
    "%2F%5Cevil.example/x",        # mixed encodings, same fold
    "http%3A%5C%5Cevil.example/x", # encoded scheme plus encoded backslashes
    "%5C%5Cgitlab.com@evil.example/x",
]


@pytest.mark.parametrize("path", STILL_NAMES_A_HOST)
def test_an_encoded_backslash_that_opens_an_authority_is_still_refused(
        path: str) -> None:
    refusal = api.path_refusal(path)
    assert refusal, f"{path!r} was accepted as an API path"
    assert refusal.startswith("ERROR")


@pytest.mark.parametrize("path", STILL_NAMES_A_HOST)
def test_it_never_reaches_glab(path: str, monkeypatch: Any,
                               capsys: Any) -> None:
    seen = _argv_glab_saw(monkeypatch, path)
    assert api.main() == 1
    assert seen == [], f"glab was invoked with {seen!r}"
    capsys.readouterr()


#: A literal backslash is not narrowed at all: it is still not path syntax.
LITERAL_BACKSLASH = [
    "\\\\evil.example\\x",
    "/\\evil.example/x",
    "projects/1/repository/files/src\\main.rs/raw?ref=main",
]


@pytest.mark.parametrize("path", LITERAL_BACKSLASH)
def test_a_literal_backslash_is_still_refused(path: str) -> None:
    assert api.path_refusal(path).startswith("ERROR")


def test_a_lone_encoded_backslash_folds_to_one_slash_and_is_accepted() -> None:
    """The boundary the fold creates, asserted rather than implied.

    `%5Cevil.example/x` decodes to a single backslash and folds to a single
    slash — a leading-slash path, which the op has always accepted. One slash
    does not open an authority; it takes two, and that is the case above.
    """
    assert api.path_refusal("%5Cevil.example/x") == ""
    assert api.decoded_host_naming_reason("/evil.example/x") == ""
    assert api.decoded_host_naming_reason("//evil.example/x") != ""


def test_the_raw_rule_is_what_refuses_a_literal_backslash() -> None:
    """The flat rule stays on the raw pass, where it is the right rule."""
    assert api.host_naming_reason("src\\main.rs") != ""
    assert api.host_naming_reason("src%5Cmain.rs") == ""
