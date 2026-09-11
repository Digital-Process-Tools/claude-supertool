"""#2427 -- `load_project_config` collapses "malformed" into "none configured".

`load_project_config()` returns `{}` for three distinct cases: no config
found, config found but malformed (JSON parse error / truncated write),
and walked to repo root with nothing found. `resolve_channel`'s narrowing
arm (`_project_level`) treats an empty/`None` project config as "the
project declared no narrowing", so a repo that deliberately narrowed a
channel to `off` whose config later became malformed falls back to the
machine-owner's wider base level -- and the receipt is byte-identical to a
repo that never configured Slack at all.

TDD: this reproduces against the CURRENT `load_project_config()` /
`resolve_channel` pair. Red = the malformed-but-narrowed case resolves to
the wide `allowlist` base level, same as a repo with no project config.
Green = it fails closed instead, and the two cases are distinguishable in
`resolve_channel`'s `detail`.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


auth = _load("presets/slack/_authorization.py", "slack_authorization_2427")


def _write_home_config(tmp_path: Path, data: dict) -> Path:
    home = tmp_path / "home"
    (home / ".config" / "supertool").mkdir(parents=True, exist_ok=True)
    (home / ".config" / "supertool" / "slack_authorization.json").write_text(
        json.dumps(data), encoding="utf-8")
    return home


def _set_home(monkeypatch, path) -> None:
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setenv("USERPROFILE", str(path))
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)
    assert Path(os.path.expanduser("~")) == Path(str(path))


def test_malformed_project_config_must_not_silently_widen_to_base_level(
    monkeypatch, tmp_path,
) -> None:
    """The bug: a project narrowed C0123 to `off`, then the tracked
    `.supertool.json` was truncated (a merge marker, a bad write). The
    machine-owner's own config grants `allowlist` for this channel. A
    malformed project config must NOT silently fall back to the wider
    machine-owner level -- it must fail closed, distinguishably from
    "no project config at all"."""
    home = _write_home_config(tmp_path, {"channels": {
        "C0123": {"level": "allowlist", "users": ["U024BE7LH"]}}})
    _set_home(monkeypatch, str(home))

    project_dir = tmp_path / "project"
    (project_dir / ".git").mkdir(parents=True)
    (project_dir / ".supertool.json").write_text(
        '{"slack": {"channels": {"C0123": "off"}<<<<<<< HEAD', encoding="utf-8")

    old_cwd = os.getcwd()
    os.chdir(project_dir)
    try:
        project_result = auth.load_project_config_result()
    finally:
        os.chdir(old_cwd)

    assert project_result.error is not None, (
        "setup bug: the malformed .supertool.json was not detected as "
        "malformed at all -- load_project_config_result().error is None"
    )

    d = auth.resolve_channel("C0123", user_id="U024BE7LH",
                             project_config=project_result.data,
                             project_config_error=project_result.error)

    assert d.level == "off", (
        f"a malformed project config that narrowed this channel silently "
        f"widened to the machine-owner's base level: {d.level!r} "
        f"(detail={d.detail!r})"
    )


def test_well_formed_narrowing_still_narrows(monkeypatch, tmp_path) -> None:
    """Positive control: the fix must not just remove the fallback and make
    every project config act as fail-closed -- a valid, well-formed
    narrowing config must still narrow correctly."""
    home = _write_home_config(tmp_path, {"channels": {
        "C0123": {"level": "allowlist", "users": ["U024BE7LH"]}}})
    _set_home(monkeypatch, str(home))

    project_dir = tmp_path / "project"
    (project_dir / ".git").mkdir(parents=True)
    (project_dir / ".supertool.json").write_text(
        json.dumps({"slack": {"channels": {"C0123": "off"}}}), encoding="utf-8")

    old_cwd = os.getcwd()
    os.chdir(project_dir)
    try:
        project_result = auth.load_project_config_result()
    finally:
        os.chdir(old_cwd)

    assert project_result.error is None
    d = auth.resolve_channel("C0123", user_id="U024BE7LH",
                             project_config=project_result.data,
                             project_config_error=project_result.error)
    assert d.level == "off"


def test_malformed_detail_is_distinguishable_from_no_project_config_at_all(
    monkeypatch, tmp_path,
) -> None:
    """The `detail` string must not be byte-identical between "a project
    narrowed this and the config is now malformed" and "this project never
    configured Slack at all" -- the same defect the issue names for the
    `resolve_channel` `detail` arms, not just for the resolved `level`."""
    home = _write_home_config(tmp_path, {"channels": {
        "C0123": {"level": "allowlist", "users": ["U024BE7LH"]}}})
    _set_home(monkeypatch, str(home))

    malformed_dir = tmp_path / "malformed"
    (malformed_dir / ".git").mkdir(parents=True)
    (malformed_dir / ".supertool.json").write_text(
        '{"slack": {"channels": {"C0123": "off"}<<<<<<< HEAD', encoding="utf-8")

    none_dir = tmp_path / "none"
    (none_dir / ".git").mkdir(parents=True)

    def _resolve(project_dir):
        old_cwd = os.getcwd()
        os.chdir(project_dir)
        try:
            result = auth.load_project_config_result()
        finally:
            os.chdir(old_cwd)
        return auth.resolve_channel("C0123", user_id="U024BE7LH",
                                     project_config=result.data,
                                     project_config_error=result.error)

    malformed = _resolve(malformed_dir)
    none = _resolve(none_dir)

    assert malformed.level == "off"
    assert none.level == "allowlist"
    assert malformed.detail != none.detail
