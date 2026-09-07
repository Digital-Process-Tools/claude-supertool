"""#2035 -- Slack channel authorization: out-of-repo, default-closed, per-channel.

TDD note: `presets/slack/_authorization.py` did not exist before this file, so
every test here started red against `ModuleNotFoundError` -- the module and
this file were written in the same change, and the red/green split for this
lane is: red = `ModuleNotFoundError: presets.slack._authorization`, green =
all tests below.

Positive controls paired with every "must not authorize" case, per the brief:
`test_a_pinned_user_may_instruct_under_allowlist` (must authorize) sits next
to `test_an_unpinned_user_may_not_instruct_under_allowlist` (must not).
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


auth = _load("presets/slack/_authorization.py", "slack_authorization_2035")


def _write_config(tmp_path: Path, data: dict) -> Path:
    home = tmp_path / "home"
    (home / ".config" / "supertool").mkdir(parents=True, exist_ok=True)
    (home / ".config" / "supertool" / "slack_authorization.json").write_text(
        json.dumps(data), encoding="utf-8")
    return home


# ---------------------------------------------------------------------------
# Property 1 -- default is the closed end
# ---------------------------------------------------------------------------

def test_no_config_file_at_all_refuses(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    d = auth.resolve_channel("C0123")
    assert d.level == "off"
    assert d.heard is False
    assert d.may_instruct is False


def test_an_unlisted_channel_refuses_even_with_other_channels_configured(
    monkeypatch, tmp_path,
) -> None:
    home = _write_config(tmp_path, {"channels": {"C_OTHER": {"level": "open"}}})
    monkeypatch.setenv("HOME", str(home))
    d = auth.resolve_channel("C0123")
    assert d.level == "off"
    assert d.heard is False


def test_a_broken_config_file_fails_closed_not_open(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    (home / ".config" / "supertool").mkdir(parents=True, exist_ok=True)
    (home / ".config" / "supertool" / "slack_authorization.json").write_text(
        "{not json", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    d = auth.resolve_channel("C0123")
    assert d.level == "off"
    assert "could not be parsed" in d.detail


# ---------------------------------------------------------------------------
# `context` -- delivered (property, not tested here -- delivery is out of
# scope for this module), never authorized
# ---------------------------------------------------------------------------

def test_context_level_is_heard_but_never_authorizes(monkeypatch, tmp_path) -> None:
    home = _write_config(tmp_path, {"channels": {"C0123": {"level": "context"}}})
    monkeypatch.setenv("HOME", str(home))
    d = auth.resolve_channel("C0123", user_id="U024BE7LH")
    assert d.level == "context"
    assert d.heard is True
    assert d.may_instruct is False


# ---------------------------------------------------------------------------
# `allowlist` -- pinned IDs only, never a live Slack group lookup
# ---------------------------------------------------------------------------

def test_a_pinned_user_may_instruct_under_allowlist(monkeypatch, tmp_path) -> None:
    """Positive control: the case allowlist exists to grant."""
    home = _write_config(tmp_path, {"channels": {
        "C0123": {"level": "allowlist", "users": ["U024BE7LH"]}}})
    monkeypatch.setenv("HOME", str(home))
    d = auth.resolve_channel("C0123", user_id="U024BE7LH")
    assert d.level == "allowlist"
    assert d.heard is True
    assert d.may_instruct is True


def test_an_unpinned_user_may_not_instruct_under_allowlist(monkeypatch, tmp_path) -> None:
    """Negative control paired with the test above: same channel, different user."""
    home = _write_config(tmp_path, {"channels": {
        "C0123": {"level": "allowlist", "users": ["U024BE7LH"]}}})
    monkeypatch.setenv("HOME", str(home))
    d = auth.resolve_channel("C0123", user_id="U999INTRUDER")
    assert d.level == "allowlist"
    assert d.heard is True, "allowlist still delivers to everyone -- it gates authority, not delivery"
    assert d.may_instruct is False


def test_a_display_name_is_not_accepted_in_place_of_a_user_id(monkeypatch, tmp_path) -> None:
    """The issue's own rule: pinned IDs, not handles -- an id is not the
    author's to pick, a display name is."""
    home = _write_config(tmp_path, {"channels": {
        "C0123": {"level": "allowlist", "users": ["U024BE7LH"]}}})
    monkeypatch.setenv("HOME", str(home))
    d = auth.resolve_channel("C0123", user_id="max")  # a handle, not a U... id
    assert d.may_instruct is False


# ---------------------------------------------------------------------------
# `open` -- declared but refused, never silently downgraded or granted
# ---------------------------------------------------------------------------

def test_open_is_refused_rather_than_silently_downgraded_to_context(
    monkeypatch, tmp_path,
) -> None:
    home = _write_config(tmp_path, {"channels": {"C0123": {"level": "open"}}})
    monkeypatch.setenv("HOME", str(home))
    d = auth.resolve_channel("C0123", user_id="anyone")
    assert d.level == "refused"
    assert d.heard is False, "refused must not silently deliver as if it were context"
    assert d.may_instruct is False
    assert "not yet implemented" in d.detail


# ---------------------------------------------------------------------------
# Property 2 -- a project's .supertool.json may narrow, never widen
# ---------------------------------------------------------------------------

def test_a_project_may_narrow_allowlist_down_to_off(monkeypatch, tmp_path) -> None:
    home = _write_config(tmp_path, {"channels": {
        "C0123": {"level": "allowlist", "users": ["U024BE7LH"]}}})
    monkeypatch.setenv("HOME", str(home))
    project = {"slack": {"channels": {"C0123": "off"}}}
    d = auth.resolve_channel("C0123", user_id="U024BE7LH", project_config=project)
    assert d.level == "off"
    assert d.heard is False


def test_a_project_cannot_widen_off_to_open(monkeypatch, tmp_path) -> None:
    """Positive control's mirror: an out-of-repo `off` stays `off` no matter
    what a tracked .supertool.json asks for -- cloning a repo must not be
    able to grant a remote channel authority over the machine that cloned
    it."""
    home = _write_config(tmp_path, {"channels": {"C0123": {"level": "off"}}})
    monkeypatch.setenv("HOME", str(home))
    project = {"slack": {"channels": {"C0123": "open"}}}
    d = auth.resolve_channel("C0123", user_id="anyone", project_config=project)
    assert d.level == "off"
    assert "WIDER" in d.detail or "ignored" in d.detail


def test_a_project_may_narrow_context_to_off_leaving_other_channels_alone(
    monkeypatch, tmp_path,
) -> None:
    home = _write_config(tmp_path, {"channels": {
        "C_NARROWED": {"level": "context"},
        "C_UNTOUCHED": {"level": "context"},
    }})
    monkeypatch.setenv("HOME", str(home))
    project = {"slack": {"channels": {"C_NARROWED": "off"}}}
    narrowed = auth.resolve_channel("C_NARROWED", project_config=project)
    untouched = auth.resolve_channel("C_UNTOUCHED", project_config=project)
    assert narrowed.level == "off"
    assert untouched.level == "context"


# ---------------------------------------------------------------------------
# Property 3 -- the resolved level is visible, not just the raw file
# ---------------------------------------------------------------------------

def test_the_detail_names_which_file_or_absence_produced_the_level(
    monkeypatch, tmp_path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "no-home"))
    d = auth.resolve_channel("C0123")
    assert "slack_authorization.json" in d.detail


# ---------------------------------------------------------------------------
# An invalid level string in the config is refused, not guessed at
# ---------------------------------------------------------------------------

def test_an_unrecognised_level_string_refuses_rather_than_guessing(
    monkeypatch, tmp_path,
) -> None:
    home = _write_config(tmp_path, {"channels": {"C0123": {"level": "SUPER_OPEN"}}})
    monkeypatch.setenv("HOME", str(home))
    d = auth.resolve_channel("C0123")
    assert d.level == "off"
    assert "not one of" in d.detail
