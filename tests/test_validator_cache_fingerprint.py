"""The validator cache key must describe the TOOLS, not only the analysed file.

Before this, a result computed by a buggy analyser kept being replayed after the
analyser was fixed: same file content, same cmd string, same key. Found when
mcp-phpstan-warm 0.6.0 -> 0.7.0 fixed two staleness bugs and the very next run
still served 0.6.0's wrong answers, in 0.2s, without doing any work.
"""
from __future__ import annotations

import os
from pathlib import Path

import supertool


def _fresh(monkeypatch, config=None):
    """Clear the per-process fingerprint memo and pin the config."""
    supertool._VALIDATOR_FINGERPRINT_CACHE.clear()
    monkeypatch.setattr(supertool, "_load_config", lambda: config or {})


def _bump_mtime(path: Path) -> None:
    """Change the file so its stat signature differs."""
    path.write_text(path.read_text() + "# changed\n")
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def test_key_is_stable_when_nothing_changed(tmp_path, monkeypatch) -> None:
    _fresh(monkeypatch)
    target = tmp_path / "Subject.php"
    target.write_text("<?php\n")
    adapter = tmp_path / "adapter.py"
    adapter.write_text("print('{}')\n")
    cmd = f"python3 {adapter}"

    first = supertool._validator_cache_key(str(target), "phpstan-mcp", cmd, {})
    supertool._VALIDATOR_FINGERPRINT_CACHE.clear()
    second = supertool._validator_cache_key(str(target), "phpstan-mcp", cmd, {})

    assert first is not None
    assert first == second, "an unchanged tool + unchanged file must still hit the cache"


def test_editing_the_adapter_changes_the_key(tmp_path, monkeypatch) -> None:
    _fresh(monkeypatch)
    target = tmp_path / "Subject.php"
    target.write_text("<?php\n")
    adapter = tmp_path / "adapter.py"
    adapter.write_text("print('{}')\n")
    cmd = f"python3 {adapter}"

    before = supertool._validator_cache_key(str(target), "phpstan-mcp", cmd, {})
    _bump_mtime(adapter)
    supertool._VALIDATOR_FINGERPRINT_CACHE.clear()
    after = supertool._validator_cache_key(str(target), "phpstan-mcp", cmd, {})

    assert before != after, "an edited adapter must not serve results it did not produce"


def test_touching_a_fingerprint_path_changes_the_key(tmp_path, monkeypatch) -> None:
    """The lockfile case: the launcher is a stable wrapper, the analyser moved."""
    _fresh(monkeypatch)
    target = tmp_path / "Subject.php"
    target.write_text("<?php\n")
    lock = tmp_path / "composer.lock"
    lock.write_text('{"packages": []}\n')
    spec = {"fingerprint_paths": [str(lock)]}
    cmd = "phpstan-wrapper"

    before = supertool._validator_cache_key(str(target), "phpstan-mcp", cmd, spec)
    _bump_mtime(lock)
    supertool._VALIDATOR_FINGERPRINT_CACHE.clear()
    after = supertool._validator_cache_key(str(target), "phpstan-mcp", cmd, spec)

    assert before != after, "a dependency upgrade must invalidate cached results"


def test_config_level_fingerprint_paths_are_honoured(tmp_path, monkeypatch) -> None:
    """Projects declare the lockfile once, not per validator."""
    target = tmp_path / "Subject.php"
    target.write_text("<?php\n")
    lock = tmp_path / "composer.lock"
    lock.write_text('{"packages": []}\n')
    config = {"validator_fingerprint_paths": [str(lock)]}

    _fresh(monkeypatch, config)
    before = supertool._validator_cache_key(str(target), "phpstan-mcp", "wrapper", {})
    _bump_mtime(lock)
    _fresh(monkeypatch, config)
    after = supertool._validator_cache_key(str(target), "phpstan-mcp", "wrapper", {})

    assert before != after


def test_missing_fingerprint_path_does_not_disable_caching(tmp_path, monkeypatch) -> None:
    """A weaker fingerprint is acceptable; refusing to cache is not."""
    _fresh(monkeypatch)
    target = tmp_path / "Subject.php"
    target.write_text("<?php\n")
    spec = {"fingerprint_paths": [str(tmp_path / "does-not-exist.lock")]}

    key = supertool._validator_cache_key(str(target), "phpstan-mcp", "wrapper", spec)

    assert key is not None


def test_key_still_tracks_file_content(tmp_path, monkeypatch) -> None:
    """The original contract survives: different content, different key."""
    _fresh(monkeypatch)
    target = tmp_path / "Subject.php"
    target.write_text("<?php\n")

    before = supertool._validator_cache_key(str(target), "phpstan-mcp", "wrapper", {})
    target.write_text("<?php\n// different\n")
    after = supertool._validator_cache_key(str(target), "phpstan-mcp", "wrapper", {})

    assert before != after


def test_spec_is_optional(tmp_path, monkeypatch) -> None:
    """Callers that predate the spec argument keep working."""
    _fresh(monkeypatch)
    target = tmp_path / "Subject.php"
    target.write_text("<?php\n")

    assert supertool._validator_cache_key(str(target), "phpstan-mcp", "wrapper") is not None


def test_unreadable_target_still_returns_none(tmp_path, monkeypatch) -> None:
    """No file, no key — a fingerprint must not paper over a missing target."""
    _fresh(monkeypatch)

    assert supertool._validator_cache_key(str(tmp_path / "gone.php"), "x", "wrapper", {}) is None
