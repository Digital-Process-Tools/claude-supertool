"""1194 — the host check decodes exactly once, and that is the decision.

`path_refusal` asks `host_naming_reason` about the literal value and
`decoded_host_naming_reason` about `urllib.parse.unquote(value)`. One decode,
never a fixed point. So `%252F%252Fevil.example/x` is accepted, and this file
exists to record that the acceptance is a decision rather than an oversight —
it was refiled as a defect twice before anything said so.

One decode covers both readers the value has:

* depth 0 — glab's own endpoint parse, which sees the literal string;
* depth 1 — a consumer that percent-decodes once and then parses.

Depth 2 would need a consumer that decodes twice and only then applies
authority semantics. Go's `url.Parse` keeps `RawPath` and `RequestURI()` puts
the original `%252F%252F...` back on the wire, so GitLab receives what
supertool sent and its router decodes once — yielding a literal path segment
of the files API, not an authority.

Decoding to a fixed point instead would refuse `%252F%252Fx`, the correct and
only encoding for a file literally named `%2F%2Fx`. That is #1043 reproduced
one level deeper: a legitimately-encoded filename made unreachable to close a
route no consumer walks.

Nothing here reaches GitLab. `glab` is unauthenticated in the dev sandbox, and
neither #1194's filer nor its auditor claimed a proven end-to-end read; these
assertions are on `path_refusal`, which is where the decision lives.
"""
from __future__ import annotations

import importlib.util
import urllib.parse
from pathlib import Path

import pytest

PRESET_PATH = Path(__file__).parent.parent / "presets" / "gitlab" / "api.py"
_spec = importlib.util.spec_from_file_location("gitlab_api_1194", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api)


#: Names a real file whose only correct spelling is doubly encoded. Each one
#: becomes an authority-looking string only after a *second* decode, which no
#: reader of this value performs.
DOUBLY_ENCODED_IS_A_FILENAME = [
    "projects/1/repository/files/%252F%252Fx/raw?ref=main",
    "projects/1/repository/files/%255C%255Cevil.example%252Fx/raw?ref=main",
    "projects/1/repository/files/http%253A%252F%252Fx/raw?ref=main",
    "%252F%252Fevil.example/x",
    "%255C%255Cevil.example/x",
]


@pytest.mark.parametrize("path", DOUBLY_ENCODED_IS_A_FILENAME)
def test_one_decode_deep_is_deliberate_so_a_doubly_encoded_name_is_accepted(
        path: str) -> None:
    assert api.path_refusal(path) == "", f"{path!r} was refused"


#: The same shapes one decode shallower, which is the depth a real consumer
#: occupies. Accepting these would be the actual defect.
ONE_DECODE_DEEP_NAMES_A_HOST = [
    "%2F%2Fevil.example/x",
    "%5C%5Cevil.example/x",
    "%5C%2Fevil.example/x",
    "http%3A%2F%2Fevil.example/x",
]


@pytest.mark.parametrize("path", ONE_DECODE_DEEP_NAMES_A_HOST)
def test_the_depth_a_consumer_occupies_is_still_refused(path: str) -> None:
    assert api.path_refusal(path).startswith("ERROR")


def test_the_second_decode_is_the_one_not_performed() -> None:
    """The boundary spelled out as the two calls that do and do not happen.

    Refusing the value below is what decoding to a fixed point would buy, and
    the cost is the only spelling a file named `%2F%2Fevil.example` has.
    """
    raw = "%252F%252Fevil.example/x"
    once = urllib.parse.unquote(raw)
    assert once == "%2F%2Fevil.example/x"
    assert api.decoded_host_naming_reason(once) == ""
    assert api.decoded_host_naming_reason(urllib.parse.unquote(once)) != ""
    assert api.path_refusal(raw) == ""


def test_neither_pass_folds_an_encoded_backslash_before_decoding() -> None:
    """`%5C` is folded on the decoded pass only, and after the one decode.

    Pinned here because a fixed-point rewrite tends to arrive as "fold, then
    decode again", which reads as harmless and refuses `src%255Cmain.rs`.
    """
    assert api.host_naming_reason("src%5Cmain.rs") == ""
    assert api.decoded_host_naming_reason("src%5Cmain.rs") == ""
    assert api.path_refusal("projects/1/repository/files/src%255Cmain.rs/raw") == ""
