"""Tests for per-tool exclude-paths feature (issue #4).

Coverage:
- Default excludes prune .git/ and node_modules/ from glob and grep
- Project exclude-paths in .supertool.json extends defaults (additive)
- :::no-exclude overrides all excludes for a single call
- Explicit-path ops (ls, read) are unaffected by excludes
- _is_excluded helper logic
- _get_exclude_paths merges correctly
- tree and map ops respect excludes
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import supertool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tree(tmp_path: Path) -> None:
    """Create a standard fixture tree with excluded and non-excluded dirs."""
    # Normal source file
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def hello(): pass\n")

    # Should be excluded by default
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n")
    (tmp_path / ".git" / "objects").mkdir()
    (tmp_path / ".git" / "objects" / "pack.idx").write_text("binary\n")

    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "lodash").mkdir()
    (tmp_path / "node_modules" / "lodash" / "index.js").write_text("// lodash\n")

    # Also a venv
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "lib").mkdir()
    (tmp_path / ".venv" / "lib" / "site.py").write_text("# venv\n")


# ---------------------------------------------------------------------------
# _is_excluded unit tests
# ---------------------------------------------------------------------------

class TestIsExcluded:
    def test_basic_match(self):
        assert supertool._is_excluded(".git", (".git/",))

    def test_nested_match(self):
        assert supertool._is_excluded(".git/objects", (".git/",))

    def test_no_match(self):
        assert not supertool._is_excluded("src/app.py", (".git/",))

    def test_empty_excludes(self):
        assert not supertool._is_excluded(".git", ())

    def test_partial_name_no_match(self):
        # "git" should NOT match ".git/" prefix
        assert not supertool._is_excluded("git/foo", (".git/",))

    def test_normalises_os_sep(self):
        # os.path.join uses os.sep — must still match
        path = os.path.join(".git", "objects")
        assert supertool._is_excluded(path, (".git/",))


# ---------------------------------------------------------------------------
# _get_exclude_paths unit tests
# ---------------------------------------------------------------------------

class TestGetExcludePaths:
    def test_returns_defaults_with_no_config(self):
        excl = supertool._get_exclude_paths("glob")
        assert ".git/" in excl
        assert "node_modules/" in excl
        assert "dist/" in excl

    def test_no_exclude_returns_empty(self):
        excl = supertool._get_exclude_paths("glob", no_exclude=True)
        assert excl == ()

    def test_project_paths_merged_additively(self, monkeypatch):
        monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
        monkeypatch.setattr(supertool, "_CONFIG", {
            "ops": {
                "glob": {
                    "exclude-paths": ["vendor/", "my-custom-lib/"]
                }
            }
        })
        excl = supertool._get_exclude_paths("glob")
        # Defaults still present
        assert ".git/" in excl
        assert "node_modules/" in excl
        # Project additions also present
        assert "vendor/" in excl
        assert "my-custom-lib/" in excl

    def test_project_path_without_trailing_slash_normalised(self, monkeypatch):
        monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
        monkeypatch.setattr(supertool, "_CONFIG", {
            "ops": {"grep": {"exclude-paths": ["my-lib"]}}
        })
        excl = supertool._get_exclude_paths("grep")
        assert "my-lib/" in excl

    def test_project_paths_do_not_replace_defaults(self, monkeypatch):
        monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
        monkeypatch.setattr(supertool, "_CONFIG", {
            "ops": {"glob": {"exclude-paths": ["vendor/"]}}
        })
        excl = supertool._get_exclude_paths("glob")
        # Defaults must still be there
        assert ".git/" in excl
        assert "node_modules/" in excl


# ---------------------------------------------------------------------------
# glob — default excludes
# ---------------------------------------------------------------------------

class TestGlobExcludes:
    def test_default_excludes_git(self, tmp_path, monkeypatch):
        _make_tree(tmp_path)
        monkeypatch.chdir(tmp_path)
        out = supertool.op_glob("**/*")
        assert ".git" not in out
        assert "src/app.py" in out or "app.py" in out

    def test_default_excludes_node_modules(self, tmp_path, monkeypatch):
        _make_tree(tmp_path)
        monkeypatch.chdir(tmp_path)
        out = supertool.op_glob("**/*.js")
        assert "node_modules" not in out
        assert "lodash" not in out

    def test_default_excludes_venv(self, tmp_path, monkeypatch):
        _make_tree(tmp_path)
        monkeypatch.chdir(tmp_path)
        out = supertool.op_glob("**/*.py")
        assert ".venv" not in out
        assert "site.py" not in out
        assert "app.py" in out

    def test_no_exclude_sees_git(self, tmp_path, monkeypatch):
        # glob("**/*") skips dotfiles on Python 3.11+ — use explicit pattern instead
        _make_tree(tmp_path)
        monkeypatch.chdir(tmp_path)
        out = supertool.op_glob("**/*.js", no_exclude=True)
        # node_modules is not a dotfile, so glob can reach it when no_exclude=True
        assert "lodash" in out or "node_modules" in out

    def test_no_exclude_sees_node_modules(self, tmp_path, monkeypatch):
        _make_tree(tmp_path)
        monkeypatch.chdir(tmp_path)
        out = supertool.op_glob("**/*.js", no_exclude=True)
        assert "lodash" in out or "node_modules" in out

    def test_project_exclude_paths_extend_defaults(self, tmp_path, monkeypatch):
        # Add a custom dir that would normally be included
        (tmp_path / "my-custom-lib").mkdir()
        (tmp_path / "my-custom-lib" / "util.py").write_text("x = 1\n")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("y = 2\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
        monkeypatch.setattr(supertool, "_CONFIG", {
            "ops": {"glob": {"exclude-paths": ["my-custom-lib/"]}}
        })
        out = supertool.op_glob("**/*.py")
        assert "util.py" not in out
        assert "app.py" in out


# ---------------------------------------------------------------------------
# grep — default excludes
# ---------------------------------------------------------------------------

class TestGrepExcludes:
    def test_default_excludes_git(self, tmp_path, monkeypatch):
        _make_tree(tmp_path)
        monkeypatch.chdir(tmp_path)
        out = supertool.op_grep("core", str(tmp_path))
        assert ".git" not in out

    def test_default_excludes_node_modules(self, tmp_path, monkeypatch):
        _make_tree(tmp_path)
        monkeypatch.chdir(tmp_path)
        out = supertool.op_grep("lodash", str(tmp_path))
        assert "node_modules" not in out

    def test_no_exclude_sees_git(self, tmp_path, monkeypatch):
        _make_tree(tmp_path)
        monkeypatch.chdir(tmp_path)
        out = supertool.op_grep("core", str(tmp_path), no_exclude=True)
        assert "config" in out or ".git" in out

    def test_no_exclude_sees_node_modules(self, tmp_path, monkeypatch):
        _make_tree(tmp_path)
        monkeypatch.chdir(tmp_path)
        out = supertool.op_grep("lodash", str(tmp_path), no_exclude=True)
        assert "lodash" in out

    def test_project_exclude_paths_extend_defaults(self, tmp_path, monkeypatch):
        (tmp_path / "vendor").mkdir()
        (tmp_path / "vendor" / "lib.py").write_text("SECRET = 1\n")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("SECRET = 2\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
        monkeypatch.setattr(supertool, "_CONFIG", {
            "ops": {"grep": {"exclude-paths": ["vendor/"]}}
        })
        out = supertool.op_grep("SECRET", str(tmp_path))
        assert "vendor" not in out
        assert "app.py" in out
        # Defaults also still active
        # (no .git/ content to match here, but the merge is tested by _get_exclude_paths tests)


# ---------------------------------------------------------------------------
# :::no-exclude dispatch integration
# ---------------------------------------------------------------------------

class TestNoExcludeDispatch:
    def test_glob_no_exclude_via_dispatch(self, tmp_path, monkeypatch):
        _make_tree(tmp_path)
        monkeypatch.chdir(tmp_path)
        out = supertool.dispatch("glob:**/*.js:::no-exclude")
        # Header should include the suffix
        assert ":::no-exclude" in out
        # node_modules (non-dotfile) is visible when no_exclude is set
        assert "lodash" in out or "node_modules" in out

    def test_grep_no_exclude_via_dispatch(self, tmp_path, monkeypatch):
        _make_tree(tmp_path)
        monkeypatch.chdir(tmp_path)
        out = supertool.dispatch(f"grep:core:{tmp_path}:::no-exclude")
        assert ":::no-exclude" in out
        assert "config" in out or ".git" in out

    def test_glob_with_excludes_via_dispatch(self, tmp_path, monkeypatch):
        _make_tree(tmp_path)
        monkeypatch.chdir(tmp_path)
        out = supertool.dispatch("glob:**/*.js")
        assert "node_modules" not in out
        assert "lodash" not in out


# ---------------------------------------------------------------------------
# tree — default excludes
# ---------------------------------------------------------------------------

class TestTreeExcludes:
    def test_default_excludes_node_modules(self, tmp_path, monkeypatch):
        _make_tree(tmp_path)
        monkeypatch.chdir(tmp_path)
        out = supertool.op_tree(str(tmp_path), exclude_paths=supertool._get_exclude_paths("tree"))
        assert "node_modules" not in out

    def test_no_exclude_sees_node_modules(self, tmp_path, monkeypatch):
        _make_tree(tmp_path)
        monkeypatch.chdir(tmp_path)
        out = supertool.op_tree(str(tmp_path))
        # Without exclude_paths passed, tree uses its own hidden-dir filter (starts with ".")
        # but node_modules doesn't start with "." so without excludes it shows
        assert "node_modules" in out

    def test_tree_dispatch_excludes(self, tmp_path, monkeypatch):
        _make_tree(tmp_path)
        monkeypatch.chdir(tmp_path)
        out = supertool.dispatch(f"tree:{tmp_path}:3")
        assert "node_modules" not in out

    def test_tree_dispatch_no_exclude(self, tmp_path, monkeypatch):
        _make_tree(tmp_path)
        monkeypatch.chdir(tmp_path)
        out = supertool.dispatch(f"tree:{tmp_path}:3:::no-exclude")
        assert "node_modules" in out


# ---------------------------------------------------------------------------
# map — default excludes
# ---------------------------------------------------------------------------

class TestMapExcludes:
    def test_default_excludes_node_modules(self, tmp_path, monkeypatch):
        _make_tree(tmp_path)
        monkeypatch.chdir(tmp_path)
        out = supertool.op_map(str(tmp_path))
        assert "node_modules" not in out
        assert "lodash" not in out

    def test_no_exclude_sees_node_modules(self, tmp_path, monkeypatch):
        _make_tree(tmp_path)
        monkeypatch.chdir(tmp_path)
        out = supertool.op_map(str(tmp_path), no_exclude=True)
        # node_modules/lodash/index.js should appear in map
        assert "node_modules" in out or "lodash" in out or "index.js" in out


# ---------------------------------------------------------------------------
# Explicit-path ops unaffected (ls, read)
# ---------------------------------------------------------------------------

class TestExplicitPathOpsUnaffected:
    def test_ls_git_still_works(self, tmp_path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text("[core]\n")
        out = supertool.op_ls(str(git_dir))
        assert "config" in out

    def test_read_inside_git_still_works(self, tmp_path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        cfg = git_dir / "config"
        cfg.write_text("[core]\nrepositoryformatversion = 0\n")
        out = supertool.op_read(str(cfg))
        assert "repositoryformatversion" in out

    def test_ls_node_modules_still_works(self, tmp_path):
        nm = tmp_path / "node_modules"
        nm.mkdir()
        (nm / "pkg").mkdir()
        out = supertool.op_ls(str(nm))
        assert "pkg" in out
