"""Regression tests for #150: hardening bundle.

Covers:
- gitcli flag smuggling — checkout / merge / blame
- regex ReDoS guards on op_grep
- format_staged / validate_staged reject symlinks
- xmllint --nonet --noent
- validator cache HMAC integrity
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import supertool

PRESETS_GIT = Path(__file__).parent.parent / "presets" / "git"


def _run_git_preset(
    script: str, *args: str, cwd: str | os.PathLike | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python3", str(PRESETS_GIT / script), *args],
        capture_output=True, text=True, timeout=10, cwd=cwd,
    )


class TestGitcliFlagSmuggling:
    def test_checkout_rejects_leading_dash_ref(self):
        r = _run_git_preset("checkout.py", "--orphan=evil")
        assert "refusing for safety" in r.stdout
        assert r.returncode != 0

    def test_checkout_allows_dash_alone(self, tmp_path):
        # "-" = previous branch in git checkout, allow it.
        # MUST run in an isolated repo: checkout.py executes a real
        # `git checkout -` against the cwd, so running it in this project's
        # checkout would switch the live repo to its previous branch and
        # revert the working tree — which is exactly what happens when the
        # full suite runs under the .githooks/pre-push hook.
        repo = tmp_path / "repo"
        repo.mkdir()
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
               "PATH": os.environ["PATH"]}
        for cmd in (
            ["git", "init", "-q", "-b", "main", str(repo)],
            ["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "init"],
            ["git", "-C", str(repo), "checkout", "-q", "-b", "feature"],
        ):
            subprocess.run(cmd, env=env, check=True, capture_output=True)
        # previous branch is now "main"; "-" must switch back to it
        r = _run_git_preset("checkout.py", "-", cwd=repo)
        assert "refusing for safety" not in r.stdout
        branch = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert branch == "main", f"`-` should switch to previous branch, on {branch!r}"

    def test_merge_rejects_dash_abort(self):
        r = _run_git_preset("merge.py", "--abort")
        assert "refusing for safety" in r.stdout
        assert r.returncode != 0

    def test_merge_rejects_strategy_smuggle(self):
        r = _run_git_preset("merge.py", "-X")
        assert "refusing for safety" in r.stdout

    def test_blame_uses_dash_separator(self):
        """The git invocation must include `--` so a path starting with
        `-` is treated as a path, not a flag. Inspect the source."""
        src = (PRESETS_GIT / "blame.py").read_text(encoding="utf-8")
        assert '"--",' in src or '"--"' in src, "blame.py must include `--` separator"


class TestRegexReDoS:
    def test_long_pattern_rejected(self):
        out = supertool.op_grep("a" * 1001, ".")
        assert "pattern too long" in out

    def test_nested_unbounded_quantifier_rejected(self):
        out = supertool.op_grep("(a+)+", ".")
        assert "nested unbounded quantifiers" in out

    def test_classic_redos_pattern_rejected(self):
        out = supertool.op_grep("(.+)*", ".")
        assert "nested unbounded quantifiers" in out

    def test_normal_pattern_passes(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "foo.txt").write_text("hello world\n")
        out = supertool.op_grep("hello", ".")
        assert "hello" in out
        assert "ERROR" not in out


class TestStagedSymlinkRejected:
    def test_format_staged_skips_symlinks(self, tmp_path, monkeypatch):
        """A staged symlink must not reach formatters that could rewrite it.

        We can't easily fake `git diff --cached` output without a real repo,
        so we check that the implementation rejects symlinks at the filter
        step. The dispatch logic uses `os.path.islink` — verified by source.
        """
        # encoding='utf-8' — supertool.py contains non-cp1252 chars (em-dash,
        # arrows) that crash the Windows default codec.
        src = Path(supertool.__file__).read_text(encoding="utf-8")
        # Both staged ops must check islink before isfile.
        # (Loose grep — exact line could shift.)
        assert "os.path.islink(p)" in src
        # And use `-z` for NUL-separated names.
        assert '"-z"' in src and '"--name-only"' in src


class TestXmllintFlags:
    def test_xmllint_uses_nonet_noent(self):
        adapter = Path(__file__).parent.parent / "validators" / "xmllint" / "xmllint.py"
        src = adapter.read_text(encoding="utf-8")
        assert '"--nonet"' in src
        assert '"--noent"' in src


class TestValidatorCacheHmac:
    @pytest.fixture
    def cache_dir(self, tmp_path, monkeypatch):
        """Point cache to tmp_path so tests don't pollute real ~/.cache."""
        # Patch Path.home() to return tmp_path so all cache paths land there.
        original_home = Path.home
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        yield tmp_path / ".cache" / "supertool" / "validators"
        monkeypatch.setattr(Path, "home", original_home)

    def test_write_then_read_roundtrip(self, cache_dir):
        data = {"tool": "fake", "ok": True, "count": 0}
        supertool._validator_cache_write("k1", data)
        roundtrip = supertool._validator_cache_read("k1")
        assert roundtrip == data

    def test_tampered_entry_rejected(self, cache_dir, tmp_path):
        data = {"tool": "fake", "ok": False, "count": 5}
        supertool._validator_cache_write("k2", data)
        cache_path = supertool._validator_cache_path("k2")
        # Tamper: flip ok=False to ok=True while keeping old mac.
        wrapped = json.loads(cache_path.read_text(encoding="utf-8"))
        wrapped["data"]["ok"] = True
        cache_path.write_text(json.dumps(wrapped))
        # Read must reject — mac no longer matches.
        assert supertool._validator_cache_read("k2") is None

    def test_legacy_unwrapped_treated_as_miss(self, cache_dir, tmp_path):
        """Pre-#150 cache entries (raw dict, no mac) are not trusted."""
        # Write raw legacy form directly.
        supertool._validator_cache_path("k3").parent.mkdir(parents=True, exist_ok=True)
        supertool._validator_cache_path("k3").write_text(
            json.dumps({"tool": "fake", "ok": True, "count": 0})
        )
        assert supertool._validator_cache_read("k3") is None

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Windows filesystem doesn't enforce Unix permission bits — chmod 0600 is a no-op",
    )
    def test_secret_file_is_mode_600(self, cache_dir):
        # Force secret generation.
        supertool._validator_cache_secret()
        secret_path = Path.home() / ".cache" / "supertool" / ".cache_key"
        assert secret_path.is_file()
        import stat as _stat
        mode = _stat.S_IMODE(secret_path.stat().st_mode)
        assert mode == 0o600, f"secret must be 0600, got {oct(mode)}"

    def test_different_secret_invalidates_cache(self, cache_dir, tmp_path):
        """If the .cache_key is rotated, old entries fail HMAC verification."""
        data = {"tool": "fake", "ok": True, "count": 0}
        supertool._validator_cache_write("k4", data)
        # Rotate the secret.
        (Path.home() / ".cache" / "supertool" / ".cache_key").unlink()
        # Force regeneration with a fresh secret.
        supertool._validator_cache_secret()
        assert supertool._validator_cache_read("k4") is None


class TestValidatorCacheTtl:
    """TTL expiry on the validator cache read path (validator_cache_ttl_hours)."""

    @pytest.fixture
    def cache_dir(self, tmp_path, monkeypatch):
        original_home = Path.home
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        yield tmp_path / ".cache" / "supertool" / "validators"
        monkeypatch.setattr(Path, "home", original_home)

    def _set_ttl(self, monkeypatch, hours):
        monkeypatch.setattr(supertool, "_load_config",
                            lambda: {"validator_cache_ttl_hours": hours})

    def test_fresh_entry_is_a_hit(self, cache_dir, monkeypatch):
        self._set_ttl(monkeypatch, 24)
        data = {"tool": "fake", "ok": True, "count": 0}
        supertool._validator_cache_write("ttl1", data)
        assert supertool._validator_cache_read("ttl1") == data

    def test_expired_entry_is_a_miss(self, cache_dir, monkeypatch):
        self._set_ttl(monkeypatch, 24)
        data = {"tool": "fake", "ok": True, "count": 0}
        supertool._validator_cache_write("ttl2", data)
        # Backdate the file mtime well past the 24h window.
        old = time.time() - 100 * 3600
        os.utime(supertool._validator_cache_path("ttl2"), (old, old))
        assert supertool._validator_cache_read("ttl2") is None

    def test_ttl_zero_disables_expiry(self, cache_dir, monkeypatch):
        self._set_ttl(monkeypatch, 0)
        data = {"tool": "fake", "ok": True, "count": 0}
        supertool._validator_cache_write("ttl3", data)
        old = time.time() - 100 * 3600
        os.utime(supertool._validator_cache_path("ttl3"), (old, old))
        assert supertool._validator_cache_read("ttl3") == data


class TestValidatorResultIsCacheable:
    """Non-deterministic engine failures must not be cached (poisoning guard)."""

    def test_ok_result_is_cacheable(self):
        assert supertool._validator_result_is_cacheable({"ok": True, "errors": []})

    def test_real_finding_is_cacheable(self):
        data = {"ok": False, "errors": [
            {"code": "rector.refactor", "msg": "Would apply SomeRector"}]}
        assert supertool._validator_result_is_cacheable(data)

    def test_core_does_not_filter_by_message_text(self):
        # Engine-glitch suppression by message moved to the adapter (config-driven,
        # validators.rector.engine_glitches). The core cache filter is generic — it
        # keys only off error codes, never message text (SCHEMA.md: "Validator core
        # never parses tool-specific output"). The adapter drops a glitch before it
        # reaches this filter; a rector.error that reaches core is a real finding.
        data = {"ok": False, "errors": [
            {"code": "rector.error",
             "msg": 'System error: "ClassReflection must be resolved for class XTest"'}]}
        assert supertool._validator_result_is_cacheable(data)

    def test_mcp_transport_error_not_cacheable(self):
        data = {"ok": False, "errors": [{"code": "mcp", "msg": "connection refused"}]}
        assert not supertool._validator_result_is_cacheable(data)

    def test_exit_code_failure_not_cacheable(self):
        data = {"ok": False, "errors": [{"code": "rector.exit", "msg": "rector exit 1"}]}
        assert not supertool._validator_result_is_cacheable(data)
