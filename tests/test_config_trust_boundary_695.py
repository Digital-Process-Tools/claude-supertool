"""Config-load trust boundary (#695): stop the walk-up at .git, and refuse a
config file that is group/world-writable or not owned by the current user.

Positive controls sit beside each refusal case so the check is proven to
discriminate rather than to refuse (or accept) everything.
"""
from __future__ import annotations

import json
import os
import stat as stat_mod
import sys
from pathlib import Path

import pytest

import supertool


def _reset_config() -> None:
    supertool._CONFIG_CHECKED = False
    supertool._CONFIG = None
    supertool._mcp_specs = {}
    supertool._CONFIG_WARNINGS.clear()


# ---------------------------------------------------------------------------
# .git ancestor stops the walk
# ---------------------------------------------------------------------------

def test_walk_stops_at_git_ancestor(tmp_path: Path, monkeypatch) -> None:
    """A .supertool.json above the nearest .git must NOT be picked up."""
    outer_cfg = tmp_path / ".supertool.json"
    outer_cfg.write_text(json.dumps({"ops": {"from_outside_repo": {"cmd": "echo x"}}}))

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    child = repo / "sub"
    child.mkdir()
    monkeypatch.chdir(child)
    _reset_config()

    result = supertool._load_config()

    assert "from_outside_repo" not in result.get("ops", {}), (
        "the walk must stop at the .git ancestor and never see the outer config"
    )


def test_walk_still_finds_config_at_git_root(tmp_path: Path, monkeypatch) -> None:
    """Positive control: a config living IN the repo root (beside .git) still loads."""
    repo = tmp_path / "repo2"
    (repo / ".git").mkdir(parents=True)
    (repo / ".supertool.json").write_text(
        json.dumps({"ops": {"inside_repo": {"cmd": "echo y"}}})
    )
    child = repo / "sub"
    child.mkdir()
    monkeypatch.chdir(child)
    _reset_config()

    result = supertool._load_config()

    assert "inside_repo" in result.get("ops", {}), (
        "a config at the repo root must still load"
    )


def test_walk_without_any_git_falls_back_to_old_behavior(tmp_path: Path, monkeypatch) -> None:
    """No .git anywhere in the chain -> walk continues to filesystem root,
    same as before this change (no repo boundary to stop at)."""
    parent_cfg = tmp_path / ".supertool.json"
    parent_cfg.write_text(json.dumps({"ops": {"from_parent": {"cmd": "echo parent"}}}))
    child_dir = tmp_path / "project"
    child_dir.mkdir()
    monkeypatch.chdir(child_dir)
    _reset_config()

    result = supertool._load_config()

    assert "from_parent" in result.get("ops", {})


# ---------------------------------------------------------------------------
# Ownership / writability
# ---------------------------------------------------------------------------

@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits only")
def test_world_writable_config_is_refused(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / ".supertool.json"
    cfg.write_text(json.dumps({"ops": {"from_evil": {"cmd": "echo evil"}}}))
    cfg.chmod(0o666)  # world-writable
    monkeypatch.chdir(tmp_path)
    _reset_config()

    result = supertool._load_config()

    assert "from_evil" not in result.get("ops", {}), (
        "a world-writable config must be refused"
    )
    assert any("writable" in w or "permission" in w.lower()
               for w in supertool._CONFIG_WARNINGS), supertool._CONFIG_WARNINGS


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits only")
def test_group_writable_config_is_refused(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / ".supertool.json"
    cfg.write_text(json.dumps({"ops": {"from_evil": {"cmd": "echo evil"}}}))
    cfg.chmod(0o664)  # group-writable
    monkeypatch.chdir(tmp_path)
    _reset_config()

    result = supertool._load_config()

    assert "from_evil" not in result.get("ops", {})


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits only")
def test_owner_only_config_loads_fine(tmp_path: Path, monkeypatch) -> None:
    """Positive control: normal 0600/0644 perms, owned by the caller, load."""
    cfg = tmp_path / ".supertool.json"
    cfg.write_text(json.dumps({"ops": {"from_legit": {"cmd": "echo ok"}}}))
    cfg.chmod(0o600)
    monkeypatch.chdir(tmp_path)
    _reset_config()

    result = supertool._load_config()

    assert "from_legit" in result.get("ops", {})


@pytest.mark.skipif(os.name != "posix", reason="POSIX uid model only")
def test_config_not_owned_by_current_user_is_refused(tmp_path: Path, monkeypatch) -> None:
    """Ownership check via a forged os.stat result -- can't chown to another
    uid without privileges, so the mismatch is simulated directly."""
    cfg = tmp_path / ".supertool.json"
    cfg.write_text(json.dumps({"ops": {"from_other_owner": {"cmd": "echo x"}}}))
    monkeypatch.chdir(tmp_path)
    _reset_config()

    real_stat = os.stat
    real_path = str(cfg)

    def fake_stat(path, *a, **kw):
        st = real_stat(path, *a, **kw)
        if os.path.abspath(str(path)) == real_path:
            forged = list(st)
            forged[stat_mod.ST_UID] = st.st_uid + 12345  # not the current user
            forged[stat_mod.ST_MODE] = stat_mod.S_IFREG | 0o600
            return os.stat_result(forged)
        return st

    monkeypatch.setattr(os, "stat", fake_stat)

    result = supertool._load_config()

    assert "from_other_owner" not in result.get("ops", {})
