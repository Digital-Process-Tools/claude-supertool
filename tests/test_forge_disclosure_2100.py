"""Tests for #2100: gh-pr-create/gh-pr-edit/gh-issue-create/gl-issue-create
never carried the #2042 AI-authorship marker -- which of this loop's own
pull requests and issues carried one depended on which lane thought of it.

Fix: `presets/_publish_safety.apply_forge_disclosure`, a second entry point
next to `apply_disclosure`. Two things differ from the publish path enough
to need a distinct function rather than reusing `apply_disclosure` as-is:

* **No length cap.** A pull request or issue body has no analogue of
  Bluesky's 300-char ceiling, so the `dropped` state has nothing to mean
  here.
* **Idempotency.** `gh-pr-edit` republishes a whole body read back from the
  forge. A naive unconditional append would stack a second marker on every
  edit that happens to re-include the first one (the common case: an editor
  starts from the published body and corrects it). `apply_forge_disclosure`
  checks whether the configured marker is already a substring of the body
  and reports `"already-present"` rather than appending again.

Conftest suppresses the marker suite-wide (`SUPERTOOL_NO_PUBLISH_DISCLOSURE=1`);
`real_defaults` below undoes that, the same pattern
`tests/test_publish_disclosure_wiring_2042.py` uses.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "presets"))
import _publish_safety  # noqa: E402


@pytest.fixture
def real_defaults(monkeypatch, tmp_path):
    monkeypatch.delenv("SUPERTOOL_NO_PUBLISH_DISCLOSURE", raising=False)
    monkeypatch.delenv("SUPERTOOL_PUBLISH_BODY_ALLOWLIST", raising=False)
    monkeypatch.chdir(tmp_path)
    if hasattr(_publish_safety, "_CACHED_CONFIG"):
        delattr(_publish_safety, "_CACHED_CONFIG")
    yield
    if hasattr(_publish_safety, "_CACHED_CONFIG"):
        delattr(_publish_safety, "_CACHED_CONFIG")


def test_first_call_appends_the_marker(real_defaults) -> None:
    """Paired with the no-double-append case below in the same fixture, so a
    silence assertion cannot pass because the marker-adding code never runs
    at all (per the brief)."""
    body, state = _publish_safety.apply_forge_disclosure("Closes #2100")
    assert state == "appended"
    assert body != "Closes #2100"
    assert "Closes #2100" in body
    assert _publish_safety._DEFAULT_DISCLOSURE_TEXT in body


def test_second_call_on_the_already_marked_body_does_not_double_append(
    real_defaults,
) -> None:
    once, _ = _publish_safety.apply_forge_disclosure("Closes #2100")
    twice, state = _publish_safety.apply_forge_disclosure(once)
    assert state == "already-present"
    assert twice == once
    assert twice.count(_publish_safety._DEFAULT_DISCLOSURE_TEXT) == 1


def test_env_var_suppresses_it(real_defaults, monkeypatch) -> None:
    monkeypatch.setenv("SUPERTOOL_NO_PUBLISH_DISCLOSURE", "1")
    body, state = _publish_safety.apply_forge_disclosure("hello")
    assert state == "suppressed"
    assert body == "hello"


def test_no_length_cap_a_long_body_still_gets_the_marker(real_defaults) -> None:
    long_body = "x" * 5000
    body, state = _publish_safety.apply_forge_disclosure(long_body)
    assert state == "appended"
    assert len(body) > 5000
    assert _publish_safety._DEFAULT_DISCLOSURE_TEXT in body
