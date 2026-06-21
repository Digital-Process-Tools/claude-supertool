"""Tests for presets/_remote_default.py — config + git-remote default resolution."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

RD_PATH = Path(__file__).parent.parent / "presets" / "_remote_default.py"
_spec = importlib.util.spec_from_file_location("remote_default", RD_PATH)
assert _spec is not None and _spec.loader is not None
rd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rd)


class TestParseRemote:
    def test_scp_form_gitlab(self):
        host, path = rd.parse_remote("git@gitlab.dp.tools:fdavid/dvsi.git")
        assert host == "gitlab.dp.tools"
        assert path == "fdavid/dvsi"

    def test_scp_form_github(self):
        host, path = rd.parse_remote("git@github.com:Digital-Process-Tools/claude-supertool.git")
        assert host == "github.com"
        assert path == "Digital-Process-Tools/claude-supertool"

    def test_https_form(self):
        host, path = rd.parse_remote("https://gitlab.dp.tools/fdavid/dvsi.git")
        assert host == "gitlab.dp.tools"
        assert path == "fdavid/dvsi"

    def test_https_no_git_suffix(self):
        host, path = rd.parse_remote("https://github.com/Owner/Repo")
        assert host == "github.com"
        assert path == "Owner/Repo"

    def test_ssh_scheme_with_port(self):
        host, path = rd.parse_remote("ssh://git@gitlab.dp.tools:2222/fdavid/dvsi.git")
        assert host == "gitlab.dp.tools"
        assert path == "fdavid/dvsi"

    def test_https_with_token_user(self):
        host, path = rd.parse_remote("https://oauth2:tok@gitlab.dp.tools/fdavid/dvsi.git")
        assert host == "gitlab.dp.tools"
        assert path == "fdavid/dvsi"

    def test_deep_gitlab_group(self):
        host, path = rd.parse_remote("git@gitlab.dp.tools:group/sub/project.git")
        assert host == "gitlab.dp.tools"
        assert path == "group/sub/project"

    def test_garbage_returns_none(self):
        assert rd.parse_remote("not a url") is None
        assert rd.parse_remote("") is None
        assert rd.parse_remote("https://host-only.com") is None


class TestConfigDefault:
    def _write_config(self, tmp_path: Path, data) -> None:
        (tmp_path / ".supertool.json").write_text(json.dumps(data))

    def test_reads_defaults(self, tmp_path, monkeypatch):
        self._write_config(tmp_path, {"defaults": {"gitlab_project": "fdavid/dvsi"}})
        monkeypatch.chdir(tmp_path)
        assert rd.config_default("gitlab_project") == "fdavid/dvsi"

    def test_missing_key_returns_none(self, tmp_path, monkeypatch):
        self._write_config(tmp_path, {"defaults": {"github_repo": "o/r"}})
        monkeypatch.chdir(tmp_path)
        assert rd.config_default("gitlab_project") is None

    def test_no_defaults_block(self, tmp_path, monkeypatch):
        self._write_config(tmp_path, {"compact": True})
        monkeypatch.chdir(tmp_path)
        assert rd.config_default("gitlab_project") is None

    def test_found_in_parent(self, tmp_path, monkeypatch):
        self._write_config(tmp_path, {"defaults": {"gitlab_project": "fdavid/dvsi"}})
        sub = tmp_path / "a" / "b"
        sub.mkdir(parents=True)
        monkeypatch.chdir(sub)
        assert rd.config_default("gitlab_project") == "fdavid/dvsi"

    def test_blank_value_treated_as_absent(self, tmp_path, monkeypatch):
        self._write_config(tmp_path, {"defaults": {"gitlab_project": "   "}})
        monkeypatch.chdir(tmp_path)
        assert rd.config_default("gitlab_project") is None

    def test_malformed_json_returns_none(self, tmp_path, monkeypatch):
        (tmp_path / ".supertool.json").write_text("{not json")
        monkeypatch.chdir(tmp_path)
        assert rd.config_default("gitlab_project") is None


class TestOriginSlug:
    def test_matches_host(self, monkeypatch):
        monkeypatch.setattr(rd, "_run_git", lambda *a, **k: "git@gitlab.dp.tools:fdavid/dvsi.git")
        assert rd.origin_slug("gitlab") == "fdavid/dvsi"

    def test_host_mismatch_returns_none(self, monkeypatch):
        monkeypatch.setattr(rd, "_run_git", lambda *a, **k: "git@github.com:o/r.git")
        assert rd.origin_slug("gitlab") is None

    def test_no_remote_returns_none(self, monkeypatch):
        monkeypatch.setattr(rd, "_run_git", lambda *a, **k: None)
        assert rd.origin_slug("github.com") is None

    def test_unparseable_remote_returns_none(self, monkeypatch):
        monkeypatch.setattr(rd, "_run_git", lambda *a, **k: "garbage")
        assert rd.origin_slug("gitlab") is None


class TestResolve:
    def test_config_wins_over_remote(self, monkeypatch):
        monkeypatch.setattr(rd, "config_default", lambda k: "fdavid/from-config")
        monkeypatch.setattr(rd, "origin_slug", lambda h: "fdavid/from-remote")
        assert rd.resolve("gitlab_project", "gitlab") == "fdavid/from-config"

    def test_falls_back_to_remote(self, monkeypatch):
        monkeypatch.setattr(rd, "config_default", lambda k: None)
        monkeypatch.setattr(rd, "origin_slug", lambda h: "fdavid/from-remote")
        assert rd.resolve("gitlab_project", "gitlab") == "fdavid/from-remote"

    def test_none_when_neither(self, monkeypatch):
        monkeypatch.setattr(rd, "config_default", lambda k: None)
        monkeypatch.setattr(rd, "origin_slug", lambda h: None)
        assert rd.resolve("gitlab_project", "gitlab") is None
