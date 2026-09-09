"""#2366 -- `presets/_publish_safety.py`'s own `.supertool.json` walk carries
the pre-#695 trust gap the core loader (`_supertool._load_config`), and the
already-hardened `presets/gitlab/_maintenance.py` (#2365) /
`presets/worktree/_common.py` (#2370) / `presets/slack/_authorization.py`
(#2416) copies of it, were each fixed to close: no `.git`-ancestor boundary
stop, and no ownership/permission check before trusting a config file found
on the walk.

Written first, red before the fix: at the commit this issue was filed
against, `_supertool_config()` walks all the way to the filesystem root with
no boundary and accepts a config regardless of who owns it or how it is
permissioned. Both tests below fail against that behavior and pass once
`_supertool_config` gains the same two guards.

Mirrors `_supertool._load_config`'s own #695 hardening, matching the
established pattern in this codebase where each preset's own walk-up loader
re-implements the check rather than sharing it (presets cannot import the
core module) -- see this module's own `_config_trust_violation` docstring.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "presets"))
import _publish_safety  # noqa: E402


@pytest.fixture
def fresh_config_cache(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    if hasattr(_publish_safety, "_CACHED_CONFIG"):
        delattr(_publish_safety, "_CACHED_CONFIG")
    yield
    if hasattr(_publish_safety, "_CACHED_CONFIG"):
        delattr(_publish_safety, "_CACHED_CONFIG")


def test_walk_stops_at_the_nearest_git_ancestor(fresh_config_cache,
                                                 tmp_path) -> None:
    """A `.supertool.json` ABOVE the repo root must never be trusted -- the
    same boundary `_supertool._load_config` enforces (#695). Without the
    boundary the walk would keep going past `.git` to `outer_config.json`'s
    directory and pick up a config belonging to a DIFFERENT project."""
    outer = tmp_path
    (outer / ".supertool.json").write_text(
        json.dumps({"no_publish_confirm": True}), encoding="utf-8")
    repo = outer / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    work = repo / "sub" / "dir"
    work.mkdir(parents=True)
    os.chdir(work)

    cfg = _publish_safety._supertool_config()

    assert cfg == {}, (
        "the outer .supertool.json, above this repo's own .git root, must "
        "not be trusted -- got: " + repr(cfg))


def test_a_group_writable_config_is_skipped_with_a_warning(
        fresh_config_cache, tmp_path, capsys) -> None:
    """POSIX only: a `.supertool.json` another local account could have
    rewritten between review and read must be skipped, not trusted -- the
    same TOCTOU shape #695 closed for the core loader."""
    if os.name != "posix":
        pytest.skip("st_uid/write bits are POSIX-only")
    if os.getuid() == 0:
        pytest.skip("root bypasses the ownership check by design")
    candidate = tmp_path / ".supertool.json"
    candidate.write_text(json.dumps({"no_publish_confirm": True}),
                          encoding="utf-8")
    candidate.chmod(0o666)  # world-writable

    cfg = _publish_safety._supertool_config()

    assert cfg == {}
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "writable" in err.lower()


def test_a_config_owned_by_this_user_and_not_writable_by_others_still_works(
        fresh_config_cache, tmp_path) -> None:
    """Positive control: the hardening must not also refuse the ordinary,
    trusted case -- a config owned by the caller with no group/world write
    bit still loads normally."""
    if os.name != "posix":
        pytest.skip("st_uid/write bits are POSIX-only")
    candidate = tmp_path / ".supertool.json"
    candidate.write_text(json.dumps({"no_publish_confirm": True}),
                          encoding="utf-8")
    candidate.chmod(0o644)

    cfg = _publish_safety._supertool_config()

    assert cfg == {"no_publish_confirm": True}


def test_an_untrusted_config_does_not_block_a_further_trusted_one_above_it(
        fresh_config_cache, tmp_path) -> None:
    """The walk must SKIP an untrusted candidate and keep going up, not
    stop there -- an untrusted file is "exactly like an absent one" per
    this function's own docstring, and every sibling implementation
    (`_supertool._load_config`, `presets/gitlab/_maintenance.py`,
    `presets/worktree/_common.py`, `presets/slack/_authorization.py`)
    falls through to the `.git`-boundary check rather than stopping the
    walk on a violation. Caught in self-review (oss:auditor spawn):
    the first cut of this fix unconditionally `break`-ed after handling
    a `.supertool.json`, whether or not it was trusted, so a stray
    world-writable file anywhere on the walk silently hid every config
    above it, all the way up to (and including) a legitimately-owned one
    inside the SAME repo."""
    if os.name != "posix":
        pytest.skip("st_uid/write bits are POSIX-only")
    if os.getuid() == 0:
        pytest.skip("root bypasses the ownership check by design")
    outer = tmp_path / ".supertool.json"
    outer.write_text(json.dumps({"no_publish_confirm": True}), encoding="utf-8")
    outer.chmod(0o644)
    sub = tmp_path / "sub"
    sub.mkdir()
    untrusted = sub / ".supertool.json"
    untrusted.write_text(json.dumps({"no_publish_confirm": False}),
                          encoding="utf-8")
    untrusted.chmod(0o666)  # world-writable -- must be skipped
    work = sub / "work"
    work.mkdir()
    os.chdir(work)

    cfg = _publish_safety._supertool_config()

    assert cfg == {"no_publish_confirm": True}, (
        f"the untrusted nearer config must be skipped and the trusted "
        f"outer one found instead -- got {cfg!r}")
