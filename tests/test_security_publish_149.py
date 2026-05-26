"""Regression tests for #149: publishing safety guards.

Covers:
- `safe_resolve_body_path` rejects file:// outside the allowlist
- `require_confirm` blocks default publish, accepts force / env / JSON opt-out
- `check_token_file_mode` rejects loose perms
- `.supertool.json` knobs (publish_body_allowlist, no_publish_confirm) work

Conftest opts the whole test suite in via env vars — these tests unset
them via monkeypatch to exercise strict mode.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "presets"))
import _publish_safety  # noqa: E402


@pytest.fixture
def strict_publish(monkeypatch):
    """Force strict mode for this test."""
    monkeypatch.delenv("SUPERTOOL_PUBLISH_BODY_ALLOWLIST", raising=False)
    monkeypatch.delenv("SUPERTOOL_NO_PUBLISH_CONFIRM", raising=False)
    # Reset cached config so changes to monkeypatched cwd take effect.
    if hasattr(_publish_safety, "_CACHED_CONFIG"):
        delattr(_publish_safety, "_CACHED_CONFIG")
    yield
    if hasattr(_publish_safety, "_CACHED_CONFIG"):
        delattr(_publish_safety, "_CACHED_CONFIG")


class TestBodyAllowlist:
    def test_rejects_etc_passwd(self, strict_publish, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit):
            _publish_safety.safe_resolve_body_path("file:///etc/passwd")
        err = capsys.readouterr().err
        assert "escapes the safety allowlist" in err

    def test_rejects_ssh_key(self, strict_publish, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit):
            _publish_safety.safe_resolve_body_path(
                "file:///Users/x/.config/bluesky/app_password"
            )
        err = capsys.readouterr().err
        assert "escapes the safety allowlist" in err

    def test_accepts_max_dir(self, strict_publish, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".max").mkdir()
        body = tmp_path / ".max" / "post.md"
        body.write_text("hello")
        resolved = _publish_safety.safe_resolve_body_path(str(body))
        assert resolved.is_file()
        assert resolved.read_text() == "hello"

    def test_accepts_drafts_dir(self, strict_publish, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "drafts").mkdir()
        body = tmp_path / "drafts" / "x.md"
        body.write_text("draft")
        resolved = _publish_safety.safe_resolve_body_path(f"file://{body}")
        assert resolved.read_text() == "draft"

    def test_env_extension_adds_to_allowlist(self, strict_publish, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SUPERTOOL_PUBLISH_BODY_ALLOWLIST", str(tmp_path / "outside"))
        (tmp_path / "outside").mkdir()
        body = tmp_path / "outside" / "x.md"
        body.write_text("body")
        resolved = _publish_safety.safe_resolve_body_path(str(body))
        assert resolved.read_text() == "body"

    def test_json_extension_adds_to_allowlist(self, strict_publish, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        import json as _json
        # Use json.dumps so Windows backslashes are properly escaped in JSON.
        (tmp_path / ".supertool.json").write_text(
            _json.dumps({"publish_body_allowlist": [(tmp_path / "custom").as_posix()]})
        )
        (tmp_path / "custom").mkdir()
        body = tmp_path / "custom" / "x.md"
        body.write_text("via json")
        resolved = _publish_safety.safe_resolve_body_path(str(body))
        assert resolved.read_text() == "via json"


class TestConfirmGate:
    def test_default_blocks_publish(self, strict_publish, capsys):
        with pytest.raises(SystemExit):
            _publish_safety.require_confirm("test_op", "hello world")
        err = capsys.readouterr().err
        assert "requires explicit confirmation" in err

    def test_force_bypasses(self, strict_publish):
        _publish_safety.require_confirm("test_op", "x", force=True)  # no raise

    def test_env_bypasses(self, strict_publish, monkeypatch):
        monkeypatch.setenv("SUPERTOOL_NO_PUBLISH_CONFIRM", "1")
        _publish_safety.require_confirm("test_op", "x")  # no raise

    def test_json_bypasses(self, strict_publish, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".supertool.json").write_text('{"no_publish_confirm": true}')
        _publish_safety.require_confirm("test_op", "x")  # no raise

    def test_json_false_stays_strict(self, strict_publish, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".supertool.json").write_text('{"no_publish_confirm": false}')
        with pytest.raises(SystemExit):
            _publish_safety.require_confirm("test_op", "x")

    def test_preview_truncated_in_error(self, strict_publish, capsys):
        long = "x" * 500
        with pytest.raises(SystemExit):
            _publish_safety.require_confirm("test_op", long)
        err = capsys.readouterr().err
        assert "..." in err  # truncation marker
        assert len(err) < 1500


class TestTokenFileMode:
    def test_rejects_world_readable(self, tmp_path, capsys):
        tok = tmp_path / "tok"
        tok.write_text("SECRET")
        tok.chmod(0o644)
        with pytest.raises(SystemExit):
            _publish_safety.check_token_file_mode(tok)
        err = capsys.readouterr().err
        assert "loose permissions" in err

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Windows chmod(0o600) is a no-op — permission bits are not enforced",
    )
    def test_accepts_owner_only(self, tmp_path):
        tok = tmp_path / "tok"
        tok.write_text("SECRET")
        tok.chmod(0o600)
        _publish_safety.check_token_file_mode(tok)  # no raise

    def test_missing_file_noop(self, tmp_path):
        # No raise — caller surfaces the right error.
        _publish_safety.check_token_file_mode(tmp_path / "missing")
