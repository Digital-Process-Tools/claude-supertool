"""#2306 -- `_publish_safety._supertool_config()` swallowed an unreadable or
malformed `.supertool.json` and returned `cfg = {}`, indistinguishable from a
config file that parses fine and simply does not set any of the publish
safety keys (`no_publish_confirm`, `publish_body_allowlist`,
`publish_disclosure_text`, `no_publish_disclosure`).

Policy chosen here matches #2308's own choice at `_merge_presets`, and is the
"warn and fall back" variant the issue names as its suggested starting
point: the safety default still applies (never a refusal, so a malformed
unrelated key does not block an unrelated publish), but the read failure is
now announced at the one choke point every publish safety reader goes
through, instead of silently voting for the default.

Would this test fail if the code did nothing? Yes -- at 05de660a,
`_supertool_config()` for an unparseable `.supertool.json` returns `{}` and
writes nothing to stderr at all.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "presets"))
import _publish_safety  # noqa: E402


@pytest.fixture
def fresh_config_cache(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    if hasattr(_publish_safety, "_CACHED_CONFIG"):
        delattr(_publish_safety, "_CACHED_CONFIG")
    yield
    if hasattr(_publish_safety, "_CACHED_CONFIG"):
        delattr(_publish_safety, "_CACHED_CONFIG")


def test_unparseable_config_is_announced_and_falls_back_to_defaults(
        fresh_config_cache, tmp_path, capsys) -> None:
    (tmp_path / ".supertool.json").write_text("{not json", encoding="utf-8")

    cfg = _publish_safety._supertool_config()

    assert cfg == {}
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert ".supertool.json" in err
    assert "default" in err.lower()


def test_config_that_is_not_a_json_object_is_announced_too(
        fresh_config_cache, tmp_path, capsys) -> None:
    """A `.supertool.json` holding a JSON array parses fine but is not a
    config object -- the existing `isinstance(cfg, dict)` guard already
    catches this shape and must warn exactly like the unparseable case."""
    (tmp_path / ".supertool.json").write_text("[1, 2, 3]", encoding="utf-8")

    cfg = _publish_safety._supertool_config()

    assert cfg == {}
    err = capsys.readouterr().err
    assert "WARNING" in err


def test_well_formed_config_is_not_announced(
        fresh_config_cache, tmp_path, capsys) -> None:
    """Must-fire's pair: a genuinely well-formed config -- even one that sets
    none of the publish safety keys -- must not trip the warning."""
    (tmp_path / ".supertool.json").write_text(
        '{"presets": []}', encoding="utf-8")

    cfg = _publish_safety._supertool_config()

    assert cfg == {"presets": []}
    err = capsys.readouterr().err
    assert "WARNING" not in err


def test_no_config_file_at_all_is_not_announced(
        fresh_config_cache, tmp_path, capsys) -> None:
    """The ordinary, overwhelmingly common case -- no `.supertool.json`
    anywhere above cwd -- is a real absence, not a read failure, and must
    stay silent."""
    cfg = _publish_safety._supertool_config()

    assert cfg == {}
    err = capsys.readouterr().err
    assert "WARNING" not in err


def test_non_utf8_config_is_announced_and_falls_back_instead_of_crashing(
        fresh_config_cache, tmp_path, capsys) -> None:
    """Self-review follow-up (oss:auditor spawn): the except clause this fix
    rewrote did not catch `UnicodeDecodeError` (a `ValueError`, not an
    `OSError`), so a `.supertool.json` that is not valid UTF-8 escaped it
    entirely and crashed the whole publish op -- the opposite of the "warn
    and fall back, never refuse" policy this function's own docstring
    states. Mirrors the identical fix `_supertool.py::_load_config` already
    carries for the sibling loader (#418)."""
    (tmp_path / ".supertool.json").write_bytes(b"\xff\xfe\x00{\"a\":1}")

    cfg = _publish_safety._supertool_config()

    assert cfg == {}
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert ".supertool.json" in err
