"""2244 -- `dot_segment_reason` must see the same decoded value the host
check beside it already does, so a percent-encoded `..` is refused exactly
like a literal one.

`path_refusal` unquotes the path once before running `decoded_host_naming_reason`
(the host check), but used to hand `dot_segment_reason` the RAW path -- and `%`
is in `_PATH_CHARS`, so it passes the character allowlist unmodified. The
result: `projects/:id/repository/../../../user` was refused, but the same
traversal spelled `projects/%2e%2e/user` or `projects/%2E%2E/user` was not.

`gl-job`'s own artifact call site is not touched by this fix -- `_fetch_
artifact_bytes` double-quotes each segment before `path_refusal` ever sees it,
so `%2e` is already `%252e` there and stays refused either way (#2244's own
issue body says so explicitly; not re-asserted here to avoid coupling this
file to that call site's unrelated quoting behaviour).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

PRESET_PATH = Path(__file__).parent.parent / "presets" / "gitlab" / "api.py"
_spec = importlib.util.spec_from_file_location("gitlab_api_2244", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api)


#: Must fire: the same traversal, spelled with a percent-encoded dot segment
#: in either case.
PERCENT_ENCODED_TRAVERSAL = [
    "projects/%2e%2e/user",
    "projects/%2E%2E/user",
    "projects/:id/x/..%2fy",
]


@pytest.mark.parametrize("path", PERCENT_ENCODED_TRAVERSAL)
def test_percent_encoded_dot_segment_is_refused(path: str) -> None:
    assert api.path_refusal(path).startswith("ERROR"), \
        f"{path!r} was not refused"


#: Must-not-regress: the literal spelling that already worked stays refused.
LITERAL_TRAVERSAL = [
    "projects/:id/repository/../../../user",
    "projects/./user",
    "projects/1/x/../y",
]


@pytest.mark.parametrize("path", LITERAL_TRAVERSAL)
def test_literal_dot_segment_is_still_refused(path: str) -> None:
    assert api.path_refusal(path).startswith("ERROR"), \
        f"{path!r} was not refused"


def test_an_ordinary_path_with_no_dot_segment_is_still_allowed() -> None:
    assert api.path_refusal("projects/1/members/all") == ""
