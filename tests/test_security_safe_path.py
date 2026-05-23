"""Regression tests for #146: _safe_path cwd containment.

These tests explicitly unset SUPERTOOL_ALLOW_OUTSIDE_CWD (which conftest.py
sets for the rest of the suite) so they exercise strict mode.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

import supertool


@pytest.fixture
def strict_mode(monkeypatch):
    """Force strict mode for this test (un-do the conftest opt-in)."""
    monkeypatch.delenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", raising=False)
    yield


class TestSafePathBasics:
    def test_cwd_relative_ok(self, strict_mode):
        # supertool.py is in cwd → allowed
        assert supertool._safe_path("supertool.py").endswith("supertool.py")

    def test_cwd_itself_ok(self, strict_mode):
        # cwd itself resolves cleanly
        assert supertool._safe_path(".") == os.path.realpath(os.getcwd())

    def test_subdir_ok(self, strict_mode):
        assert supertool._safe_path("tests/conftest.py").endswith("conftest.py")

    def test_absolute_outside_cwd_rejected(self, strict_mode):
        with pytest.raises(supertool.SecurityError, match="escapes cwd"):
            supertool._safe_path("/etc/passwd")

    def test_tilde_outside_cwd_rejected(self, strict_mode):
        # ~/.ssh/ is outside any reasonable cwd
        with pytest.raises(supertool.SecurityError, match="escapes cwd"):
            supertool._safe_path("~/.ssh/id_rsa")

    def test_dotdot_traversal_rejected(self, strict_mode, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(supertool.SecurityError, match="escapes cwd"):
            supertool._safe_path("../../etc/passwd")

    def test_symlink_crossing_rejected(self, strict_mode, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # Create a symlink inside cwd that points outside
        link = tmp_path / "outside-link.txt"
        link.symlink_to("/etc/hosts")
        with pytest.raises(supertool.SecurityError, match="escapes cwd"):
            supertool._safe_path(str(link))

    def test_nul_byte_rejected(self, strict_mode):
        with pytest.raises(supertool.SecurityError, match="NUL byte"):
            supertool._safe_path("foo\x00.txt")

    def test_env_opt_out(self, monkeypatch):
        monkeypatch.setenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", "1")
        # /etc/passwd resolves cleanly when opt-out is set
        assert supertool._safe_path("/etc/passwd") == os.path.realpath("/etc/passwd")

    def test_per_call_opt_out(self, strict_mode):
        # Explicit allow_outside_cwd=True overrides strict mode
        assert supertool._safe_path("/etc/passwd", allow_outside_cwd=True) == os.path.realpath("/etc/passwd")


class TestDispatchRejectsOutsideCwd:
    """Dispatch-level enforcement — each op type checked."""

    def test_read_outside_cwd(self, strict_mode):
        out = supertool.dispatch("read:/etc/passwd")
        assert "ERROR" in out
        assert "escapes cwd" in out

    def test_head_outside_cwd(self, strict_mode):
        out = supertool.dispatch("head:/etc/passwd")
        assert "ERROR" in out and "escapes cwd" in out

    def test_tail_outside_cwd(self, strict_mode):
        out = supertool.dispatch("tail:/etc/passwd")
        assert "ERROR" in out and "escapes cwd" in out

    def test_wc_outside_cwd(self, strict_mode):
        out = supertool.dispatch("wc:/etc/passwd")
        assert "ERROR" in out and "escapes cwd" in out

    def test_stat_outside_cwd(self, strict_mode):
        out = supertool.dispatch("stat:/etc/passwd")
        assert "ERROR" in out and "escapes cwd" in out

    def test_ls_outside_cwd(self, strict_mode):
        out = supertool.dispatch("ls:/etc")
        assert "ERROR" in out and "escapes cwd" in out

    def test_diff_outside_cwd(self, strict_mode):
        out = supertool.dispatch("diff:/etc/passwd:/etc/hosts")
        assert "ERROR" in out and "escapes cwd" in out

    def test_grep_outside_cwd(self, strict_mode):
        out = supertool.dispatch("grep:root:/etc")
        assert "ERROR" in out and "escapes cwd" in out


class TestPasteEditRejected:
    """Mutation ops must reject outside-cwd paths (#146 highest-impact threat)."""

    def test_paste_to_home_ssh_rejected(self, strict_mode):
        out = supertool.dispatch(
            "paste:::/Users/floriandavid/.ssh/authorized_keys-attack:::pwned\n"
        )
        assert "ERROR" in out and "escapes cwd" in out

    def test_paste_to_etc_rejected(self, strict_mode):
        out = supertool.dispatch("paste:::/etc/evil.conf:::pwned")
        assert "ERROR" in out and "escapes cwd" in out

    def test_atomic_write_direct_call_rejected(self, strict_mode):
        # Even bypassing dispatch — _atomic_write chokepoint catches it.
        with pytest.raises(supertool.SecurityError, match="escapes cwd"):
            supertool._atomic_write("/tmp/should-not-be-written.txt", "pwned")

    def test_render_file_direct_call_returns_error(self, strict_mode):
        # Same chokepoint for reads.
        out = supertool.render_file("/etc/passwd")
        assert "ERROR" in out and "escapes cwd" in out


class TestExcludeList:
    """Default exclude list shields credential dirs from traversal ops."""

    def test_max_dir_excluded_from_grep(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".max").mkdir()
        (tmp_path / ".max" / "hashnode-token.txt").write_text("UNIQUE_NEEDLE_XYZ\n")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "code.py").write_text("# benign file\n")
        out = supertool.dispatch("grep:UNIQUE_NEEDLE_XYZ:.")
        # Token file contained the needle; if grep traversed .max/ it would surface.
        # Exclude means the needle is NOT found anywhere (0 results).
        assert "0 results" in out or "no results" in out, f"credential leaked: {out}"

    def test_ssh_dir_excluded_from_tree(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".ssh").mkdir()
        (tmp_path / ".ssh" / "id_rsa").write_text("PRIVATE KEY\n")
        (tmp_path / "ok.py").write_text("pass\n")
        out = supertool.dispatch("tree:.")
        assert ".ssh" not in out
        assert "id_rsa" not in out


class TestEnvVarSemantics:
    def test_env_var_0_treated_as_strict(self, monkeypatch):
        """Only the literal string "1" disables strict mode. "0", "false",
        "no" stay strict — safer to fail closed."""
        monkeypatch.setenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", "0")
        with pytest.raises(supertool.SecurityError, match="escapes cwd"):
            supertool._safe_path("/etc/passwd")

    def test_env_var_false_treated_as_strict(self, monkeypatch):
        monkeypatch.setenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", "false")
        with pytest.raises(supertool.SecurityError, match="escapes cwd"):
            supertool._safe_path("/etc/passwd")

    def test_env_var_empty_treated_as_strict(self, monkeypatch):
        monkeypatch.setenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", "")
        with pytest.raises(supertool.SecurityError, match="escapes cwd"):
            supertool._safe_path("/etc/passwd")
