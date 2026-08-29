"""Tests for #2042: a published message carries no marker that a machine
wrote it, and the user-token route makes it indistinguishable from a human
posting.

Fix: `presets/_publish_safety.apply_disclosure` appends a short ASCII
marker to a publish body, on the shared publish path `publish_body_allowlist`
already lives on (`presets/_publish_safety.py:79`). On by default -- silence
about authorship must be a decision somebody recorded (env var or
`.supertool.json`), never a side effect of the body simply being long.

Conftest suppresses the marker suite-wide (`SUPERTOOL_NO_PUBLISH_DISCLOSURE=1`,
set the same way `SUPERTOOL_NO_PUBLISH_CONFIRM` is) so pre-#2042 tests that
pin an exact posted body do not need to know about this. `strict_disclosure`
below undoes that isolation the same way `test_security_publish_149.py`'s
`strict_publish` fixture undoes the confirm one, so the on-by-default claim
is tested against real defaults, not the suite's own override of them.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "presets"))
import _publish_safety  # noqa: E402


@pytest.fixture
def strict_disclosure(monkeypatch, tmp_path):
    monkeypatch.delenv("SUPERTOOL_NO_PUBLISH_DISCLOSURE", raising=False)
    monkeypatch.delenv("SUPERTOOL_PUBLISH_BODY_ALLOWLIST", raising=False)
    monkeypatch.chdir(tmp_path)
    if hasattr(_publish_safety, "_CACHED_CONFIG"):
        delattr(_publish_safety, "_CACHED_CONFIG")
    yield
    if hasattr(_publish_safety, "_CACHED_CONFIG"):
        delattr(_publish_safety, "_CACHED_CONFIG")


def test_default_appends_the_marker_with_no_configuration_at_all(strict_disclosure) -> None:
    """The assertion most likely to be written vacuously (per the brief):
    with nothing set, nothing chdir'd into a config, the marker must still
    be appended -- "on by default" means silence is the state you get
    without deciding anything."""
    body, state = _publish_safety.apply_disclosure("Deploy finished, all green.")
    assert state == "appended"
    assert body != "Deploy finished, all green."
    assert "Deploy finished, all green." in body
    assert body.encode("ascii")  # must survive a cp1252 console -- #2066


def test_env_var_suppresses_it(strict_disclosure, monkeypatch) -> None:
    """Pairs with the default-on test above, in the same fixture, so the
    default cannot be honestly tested by a harness that always suppresses:
    suppression must actually suppress, and it must be a decision findable
    in the environment or `.supertool.json`."""
    monkeypatch.setenv("SUPERTOOL_NO_PUBLISH_DISCLOSURE", "1")
    body, state = _publish_safety.apply_disclosure("hello")
    assert state == "suppressed"
    assert body == "hello"


def test_json_config_suppresses_it(strict_disclosure, tmp_path) -> None:
    (tmp_path / ".supertool.json").write_text('{"no_publish_disclosure": true}')
    body, state = _publish_safety.apply_disclosure("hello")
    assert state == "suppressed"
    assert body == "hello"


def test_json_config_can_override_the_marker_text(strict_disclosure, tmp_path) -> None:
    (tmp_path / ".supertool.json").write_text(
        '{"publish_disclosure_text": "[bot]"}'
    )
    body, state = _publish_safety.apply_disclosure("hello")
    assert state == "appended"
    assert "[bot]" in body
    assert "hello" in body


def test_marker_is_dropped_rather_than_truncating_the_body_when_it_does_not_fit(
    strict_disclosure,
) -> None:
    """Bluesky's 300-char limit makes this concrete: a body already at the
    limit cannot also carry the marker. Dropping the marker is a distinct,
    named state -- not silent truncation of the body, and not a silent
    disappearance indistinguishable from "nobody configured a marker"."""
    body = "x" * 300
    result, state = _publish_safety.apply_disclosure(body, max_len=300)
    assert state == "dropped"
    assert result == body  # body itself is never touched


def test_marker_fits_within_max_len_when_there_is_room(strict_disclosure) -> None:
    body = "short post"
    result, state = _publish_safety.apply_disclosure(body, max_len=300)
    assert state == "appended"
    assert len(result) <= 300
    assert "short post" in result


def test_non_ascii_override_falls_back_to_the_default_marker(
    strict_disclosure, tmp_path, capsys,
) -> None:
    """The docstring claims the marker is "ASCII by construction" -- true
    only of the shipped default, not of an operator-supplied override
    (found in review, #2042). A non-ASCII `publish_disclosure_text` must
    not reach a `print()` unchecked -- on a non-UTF-8 console codepage
    that raises `UnicodeEncodeError` after the publish already happened
    (#2066's own class). Falling back to the default is the fix; silently
    accepting it would be the same defect this issue was filed over, one
    layer down."""
    (tmp_path / ".supertool.json").write_text(
        '{"publish_disclosure_text": "— written by a robot"}',
        encoding="utf-8",
    )
    body, state = _publish_safety.apply_disclosure("hello")
    assert state == "appended"
    assert body.encode("ascii")  # never a non-ASCII marker on the wire
    assert _publish_safety._DEFAULT_DISCLOSURE_TEXT in body
    assert "not ASCII" in capsys.readouterr().err
