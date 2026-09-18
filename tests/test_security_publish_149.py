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
import stat
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "presets"))
import _publish_safety  # noqa: E402


@pytest.fixture
def strict_publish(monkeypatch, tmp_path):
    """Force strict mode for this test.

    Env AND cwd both feed the gate (`.supertool.json` is found by walking up
    from cwd — see `_publish_safety._supertool_config`), so both have to be
    isolated or "strict" silently means "whatever this checkout's own
    `.supertool.json` happens to say" (#1897). The chdir here is what makes
    that isolation apply to every test using this fixture, not just the
    ones that remember to chdir themselves.
    """
    monkeypatch.delenv("SUPERTOOL_PUBLISH_BODY_ALLOWLIST", raising=False)
    monkeypatch.delenv("SUPERTOOL_NO_PUBLISH_CONFIRM", raising=False)
    monkeypatch.chdir(tmp_path)
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
        assert resolved.read_text(encoding="utf-8") == "hello"

    def test_accepts_drafts_dir(self, strict_publish, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "drafts").mkdir()
        body = tmp_path / "drafts" / "x.md"
        body.write_text("draft")
        resolved = _publish_safety.safe_resolve_body_path(f"file://{body}")
        assert resolved.read_text(encoding="utf-8") == "draft"

    def test_env_extension_adds_to_allowlist(self, strict_publish, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SUPERTOOL_PUBLISH_BODY_ALLOWLIST", str(tmp_path / "outside"))
        (tmp_path / "outside").mkdir()
        body = tmp_path / "outside" / "x.md"
        body.write_text("body")
        resolved = _publish_safety.safe_resolve_body_path(str(body))
        assert resolved.read_text(encoding="utf-8") == "body"

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
        assert resolved.read_text(encoding="utf-8") == "via json"


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
    """Three states, and the third was found by CI on Windows (#227).

    `test_rejects_world_readable` used to carry no gate and passed on
    Windows for the wrong reason: every file there reports `0o666`, so the
    refusal fired whatever the `chmod` did, and the test could not tell a
    working guard from one that refuses everything. It refused everything.
    Nothing noticed because the function had no production caller until
    #227, which would have made the YouTube write ops exit 2 on every
    Windows machine.

    The gate below is a capability probe rather than `sys.platform ==
    "win32"` -- the same reasoning `tests/_symlink.py` sets out, and the
    same probe the function itself now uses to decide.
    """

    def test_rejects_world_readable(self, tmp_path, capsys):
        """MUST FIRE, where the bits mean something."""
        if not _publish_safety._mode_bits_are_enforced():
            pytest.skip("this filesystem does not enforce POSIX mode bits, so "
                        "0o644 is not a fact about who can read the file")
        tok = tmp_path / "tok"
        tok.write_text("SECRET")
        tok.chmod(0o644)
        with pytest.raises(SystemExit):
            _publish_safety.check_token_file_mode(tok)
        err = capsys.readouterr().err
        assert "loose permissions" in err

    def test_a_loose_mode_is_a_warning_where_the_bits_are_not_enforced(
            self, tmp_path, capsys, monkeypatch):
        """The third state, exercised on EVERY platform.

        Forcing the probe rather than waiting for a Windows runner: this is
        the arm that decides whether the feature works there at all, and a
        test only one of twelve legs can reach is a test that finds this
        the way CI just did.
        """
        monkeypatch.setattr(_publish_safety, "_mode_bits_are_enforced",
                            lambda *a, **k: False)
        tok = tmp_path / "tok"
        tok.write_text("SECRET")
        tok.chmod(0o644)
        _publish_safety.check_token_file_mode(tok)  # must NOT raise
        err = capsys.readouterr().err
        assert "could not verify" in err, (
            "an unverifiable credential must say so; silence here is the "
            "absence-read-as-clean defect")
        assert "UNKNOWN" in err
        assert "loose permissions" not in err, (
            "this is not a finding about the file's permissions -- blaming "
            "the operator for a fact about the platform sends them to "
            "`chmod` on an OS that has none")

    def test_accepts_owner_only(self, tmp_path, capsys):
        """MUST NOT FIRE, on any platform.

        Ungated on purpose, unlike the old `skipif`: a 0o600 file is a pass
        everywhere, because the tightness check runs before the probe. That
        is what keeps the warning above from reaching a file that is
        already fine.
        """
        tok = tmp_path / "tok"
        tok.write_text("SECRET")
        tok.chmod(0o600)
        _publish_safety.check_token_file_mode(tok)  # no raise
        if _publish_safety._mode_bits_are_enforced():
            assert capsys.readouterr().err == "", (
                "a tight file must produce no diagnostic at all")

    def test_the_probe_agrees_with_the_filesystem(self, tmp_path):
        """Positive control for the probe itself.

        Everything above branches on `_mode_bits_are_enforced()`, so a probe
        stuck on one answer would make one arm vacuous and the other
        unreachable without either going red.
        """
        tok = tmp_path / "tok"
        tok.write_text("x")
        tok.chmod(0o600)
        survived = stat.S_IMODE(os.stat(tok).st_mode) == 0o600
        assert _publish_safety._mode_bits_are_enforced() == survived, (
            "the probe disagrees with what this filesystem just did to a "
            "real chmod")

    def test_missing_file_noop(self, tmp_path):
        # No raise — caller surfaces the right error.
        _publish_safety.check_token_file_mode(tmp_path / "missing")

    def test_probe_is_created_next_to_the_credential_not_in_tmpdir(
            self, tmp_path, monkeypatch):
        """#2597: the probe must measure the credential's own filesystem.

        `tempfile.mkstemp()` with no `dir=` lands in `TMPDIR`, which can be
        a different mount (exFAT/FAT/SMB, a container `/tmp`) than wherever
        the credential actually lives. A real chmod on the credential's own
        directory is the only thing worth asking the question about.
        """
        _publish_safety._mode_bits_are_enforced.cache_clear()
        cred_dir = tmp_path / "creds"
        cred_dir.mkdir()
        tok = cred_dir / "tok"
        tok.write_text("SECRET")
        tok.chmod(0o644)

        seen_dirs = []
        real_mkstemp = tempfile.mkstemp

        def spy_mkstemp(*args, **kwargs):
            seen_dirs.append(kwargs.get("dir"))
            return real_mkstemp(*args, **kwargs)

        monkeypatch.setattr(tempfile, "mkstemp", spy_mkstemp)
        with pytest.raises(SystemExit):
            _publish_safety.check_token_file_mode(tok)
        assert seen_dirs and seen_dirs[0] == str(cred_dir), (
            f"probe was created in {seen_dirs!r}, not the credential's own "
            f"directory {cred_dir} -- the filesystem judged is not the one "
            "the credential lives on")

    def test_probe_falls_back_and_reports_when_credential_dir_is_unusable(
            self, tmp_path, monkeypatch, capsys):
        """The fallback (#2597's own fix shape) must be visible, not silent.

        A credential path whose directory does not exist (or is not
        writable) cannot be probed there -- falling back to the old TMPDIR
        behaviour is fine, but doing so without saying so reproduces the
        exact silent-mismeasurement defect this issue is about.
        """
        _publish_safety._mode_bits_are_enforced.cache_clear()
        missing_dir_tok = tmp_path / "does-not-exist" / "tok"

        seen_dirs = []
        real_mkstemp = tempfile.mkstemp

        def spy_mkstemp(*args, **kwargs):
            seen_dirs.append(kwargs.get("dir"))
            return real_mkstemp(*args, **kwargs)

        monkeypatch.setattr(tempfile, "mkstemp", spy_mkstemp)
        # The file itself doesn't need to exist for the probe-dir choice to
        # be made -- exercise the helper directly.
        chosen = _publish_safety._probe_dir_for(missing_dir_tok)
        assert chosen != str(missing_dir_tok.parent)
        err = capsys.readouterr().err
        assert "cannot probe" in err or "falling back" in err, (
            "the fallback away from the credential's own directory must be "
            "reported, not silently swallowed")
