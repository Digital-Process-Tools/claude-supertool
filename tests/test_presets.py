"""Tests for the preset system — loading, merging, path resolution."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import supertool


class TestFindPresetFile:
    """Preset file resolution: project > user > shipped."""

    def test_project_dir_wins(self, tmp_path: Path) -> None:
        """A preset in project/presets/ takes priority over shipped."""
        proj = tmp_path / "project" / "presets"
        proj.mkdir(parents=True)
        (proj / "test.json").write_text('{"ops":{}}')
        result = supertool._find_preset_file("test", str(tmp_path / "project"))
        assert result == str(proj / "test.json")

    def test_user_dir_second(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Falls back to ~/.config/supertool/presets/."""
        user_dir = tmp_path / "home" / ".config" / "supertool" / "presets"
        user_dir.mkdir(parents=True)
        (user_dir / "custom.json").write_text('{"ops":{}}')
        monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path / "home") if p == "~" else p)
        result = supertool._find_preset_file("custom", str(tmp_path / "empty-project"))
        assert result == str(user_dir / "custom.json")

    def test_shipped_dir_last(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Falls back to supertool install dir."""
        shipped = tmp_path / "install" / "presets"
        shipped.mkdir(parents=True)
        (shipped / "gitlab.json").write_text('{"ops":{}}')
        monkeypatch.setattr(supertool, "_INSTALL_DIR", str(tmp_path / "install"))
        result = supertool._find_preset_file("gitlab", str(tmp_path / "no-project"))
        assert result == str(shipped / "gitlab.json")

    def test_not_found_returns_none(self, tmp_path: Path) -> None:
        """Returns None when preset doesn't exist anywhere."""
        result = supertool._find_preset_file("nonexistent", str(tmp_path))
        assert result is None

    def test_project_overrides_shipped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Project preset wins over shipped preset with same name."""
        proj = tmp_path / "project" / "presets"
        proj.mkdir(parents=True)
        (proj / "gitlab.json").write_text('{"ops":{"issue":{"cmd":"echo PROJECT"}}}')

        shipped = tmp_path / "install" / "presets"
        shipped.mkdir(parents=True)
        (shipped / "gitlab.json").write_text('{"ops":{"issue":{"cmd":"echo SHIPPED"}}}')

        monkeypatch.setattr(supertool, "_INSTALL_DIR", str(tmp_path / "install"))
        result = supertool._find_preset_file("gitlab", str(tmp_path / "project"))
        assert result == str(proj / "gitlab.json")


class TestMergePresets:
    """Preset merging into config."""

    def test_no_presets_key_noop(self) -> None:
        """Config without presets key is unchanged."""
        config: dict = {"ops": {"phpstan": {"cmd": "echo phpstan"}}}
        supertool._merge_presets(config, "/tmp")
        assert config["ops"] == {"phpstan": {"cmd": "echo phpstan"}}

    def test_empty_presets_list_noop(self) -> None:
        """Empty presets list is unchanged."""
        config: dict = {"presets": [], "ops": {"x": {"cmd": "echo x"}}}
        supertool._merge_presets(config, "/tmp")
        assert config["ops"] == {"x": {"cmd": "echo x"}}

    def test_preset_ops_merged(self, tmp_path: Path) -> None:
        """Preset ops are added to config."""
        presets_dir = tmp_path / "presets"
        presets_dir.mkdir()
        (presets_dir / "test.json").write_text(json.dumps({
            "ops": {"hello": {"cmd": "echo hi"}}
        }))
        config: dict = {"presets": ["test"], "ops": {}}
        supertool._merge_presets(config, str(tmp_path))
        assert "hello" in config["ops"]
        assert config["ops"]["hello"]["cmd"] == "echo hi"

    def test_project_ops_win_over_preset(self, tmp_path: Path) -> None:
        """Project-level ops override preset ops on name conflict."""
        presets_dir = tmp_path / "presets"
        presets_dir.mkdir()
        (presets_dir / "test.json").write_text(json.dumps({
            "ops": {"tool": {"cmd": "echo FROM_PRESET"}}
        }))
        config: dict = {
            "presets": ["test"],
            "ops": {"tool": {"cmd": "echo FROM_PROJECT"}}
        }
        supertool._merge_presets(config, str(tmp_path))
        assert config["ops"]["tool"]["cmd"] == "echo FROM_PROJECT"

    def test_multiple_presets_merged(self, tmp_path: Path) -> None:
        """Multiple presets merge their ops together."""
        presets_dir = tmp_path / "presets"
        presets_dir.mkdir()
        (presets_dir / "a.json").write_text(json.dumps({
            "ops": {"op_a": {"cmd": "echo A"}}
        }))
        (presets_dir / "b.json").write_text(json.dumps({
            "ops": {"op_b": {"cmd": "echo B"}}
        }))
        config: dict = {"presets": ["a", "b"], "ops": {}}
        supertool._merge_presets(config, str(tmp_path))
        assert "op_a" in config["ops"]
        assert "op_b" in config["ops"]

    def test_later_preset_overrides_earlier(self, tmp_path: Path) -> None:
        """When two presets define the same op, later one wins."""
        presets_dir = tmp_path / "presets"
        presets_dir.mkdir()
        (presets_dir / "a.json").write_text(json.dumps({
            "ops": {"tool": {"cmd": "echo A"}}
        }))
        (presets_dir / "b.json").write_text(json.dumps({
            "ops": {"tool": {"cmd": "echo B"}}
        }))
        config: dict = {"presets": ["a", "b"], "ops": {}}
        supertool._merge_presets(config, str(tmp_path))
        assert config["ops"]["tool"]["cmd"] == "echo B"

    def test_missing_preset_warns(self, tmp_path: Path) -> None:
        """Missing preset adds a warning, doesn't crash."""
        config: dict = {"presets": ["nonexistent"], "ops": {}}
        supertool._merge_presets(config, str(tmp_path))
        assert "_preset_warnings" in config
        assert "nonexistent" in config["_preset_warnings"][0]

    def test_invalid_json_warns(self, tmp_path: Path) -> None:
        """Malformed preset JSON adds a warning."""
        presets_dir = tmp_path / "presets"
        presets_dir.mkdir()
        (presets_dir / "bad.json").write_text("{not valid json")
        config: dict = {"presets": ["bad"], "ops": {}}
        supertool._merge_presets(config, str(tmp_path))
        assert "_preset_warnings" in config

    def test_no_ops_in_config_creates_ops(self, tmp_path: Path) -> None:
        """If config has no ops key, preset ops create it."""
        presets_dir = tmp_path / "presets"
        presets_dir.mkdir()
        (presets_dir / "test.json").write_text(json.dumps({
            "ops": {"greet": {"cmd": "echo hi"}}
        }))
        config: dict = {"presets": ["test"]}
        supertool._merge_presets(config, str(tmp_path))
        assert config["ops"]["greet"]["cmd"] == "echo hi"


class TestResolvePresetCmd:
    """Path resolution via {path} placeholder in preset cmds."""

    def test_path_placeholder_replaced(self) -> None:
        """{path} is replaced with preset dir + trailing slash."""
        result = supertool._resolve_preset_cmd(
            "python3 {path}gitlab/issue.py {arg}",
            "/opt/supertool/presets",
        )
        assert result == "python3 /opt/supertool/presets/gitlab/issue.py {arg}"

    def test_path_trailing_slash_normalized(self) -> None:
        """Preset dir with trailing slash doesn't double up."""
        result = supertool._resolve_preset_cmd(
            "python3 {path}run.py",
            "/opt/presets/",
        )
        assert result == "python3 /opt/presets/run.py"

    def test_no_path_placeholder_unchanged(self) -> None:
        """Commands without {path} are unchanged."""
        cmd = "echo {arg}"
        result = supertool._resolve_preset_cmd(cmd, "/some/dir")
        assert result == cmd

    def test_absolute_path_with_placeholder(self) -> None:
        """Even with {path}, other absolute paths stay untouched."""
        result = supertool._resolve_preset_cmd(
            "python3 {path}tool.py /usr/bin/config.json",
            "/opt/presets",
        )
        assert "/opt/presets/tool.py" in result
        assert "/usr/bin/config.json" in result

    def test_path_placeholder_not_in_cmd(self) -> None:
        """A cmd without {path} is unchanged."""
        result = supertool._resolve_preset_cmd("bash run.sh {arg}", "/some/dir")
        assert result == "bash run.sh {arg}"


class TestPresetDispatchIntegration:
    """End-to-end: preset ops dispatched via the main dispatch function."""

    def test_preset_op_dispatched(self, tmp_path: Path) -> None:
        """A preset op runs through dispatch like any custom op."""
        presets_dir = tmp_path / "presets"
        presets_dir.mkdir()
        (presets_dir / "test.json").write_text(json.dumps({
            "ops": {"greet": {"cmd": "echo hello {arg}"}}
        }))
        supertool._CONFIG = {
            "presets": ["test"],
            "ops": {}
        }
        supertool._merge_presets(supertool._CONFIG, str(tmp_path))
        out = supertool.dispatch("greet:world")
        assert "hello" in out
        assert "world" in out

    def test_preset_op_with_project_ops(self, tmp_path: Path) -> None:
        """Preset ops coexist with project ops."""
        presets_dir = tmp_path / "presets"
        presets_dir.mkdir()
        (presets_dir / "ext.json").write_text(json.dumps({
            "ops": {"remote": {"cmd": "echo from_preset"}}
        }))
        supertool._CONFIG = {
            "presets": ["ext"],
            "ops": {"local": {"cmd": "echo from_project"}}
        }
        supertool._merge_presets(supertool._CONFIG, str(tmp_path))
        out_remote = supertool.dispatch("remote:x")
        out_local = supertool.dispatch("local:x")
        assert "from_preset" in out_remote
        assert "from_project" in out_local
