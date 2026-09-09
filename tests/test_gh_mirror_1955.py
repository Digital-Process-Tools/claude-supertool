"""Write-through mirror for `gh-issue` reads (#1955): `presets/_mirror.py`.

TDD per the brief: these are written and run RED (module does not exist yet)
before `presets/_mirror.py` is implemented. See the developer report for the
red output; this file exercises the pure storage layer directly, matching the
codebase convention of loading a preset module by path and calling its
functions rather than shelling out (`tests/test_gh_issue_attachment_root_1506.py`
is the reference).

Three states throughout, matching the issue's own bar: `cached (age)`,
`not-cached`, `mirror-unreadable` -- and the third must never render as
either of the first two. Every "must not serve stale/wrong" case is paired
with a "must serve when genuinely cached" case in the same class of fixture,
per the brief's own rule about negative assertions needing a positive
control.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "presets"))
import _mirror  # noqa: E402


# ---------------------------------------------------------------------------
# load_config: the three states of the walk-up loader
# ---------------------------------------------------------------------------

def test_no_supertool_json_anywhere_is_not_configured(tmp_path) -> None:
    cfg = _mirror.load_config(tmp_path)
    assert cfg.path is None
    assert cfg.error is None


def test_supertool_json_with_no_mirror_key_is_not_configured(tmp_path) -> None:
    (tmp_path / ".supertool.json").write_text(json.dumps({"presets": ["github"]}), encoding="utf-8")
    cfg = _mirror.load_config(tmp_path)
    assert cfg.path is None
    assert cfg.error is None


def test_configured_key_resolves_relative_to_the_config_file_not_the_caller(tmp_path) -> None:
    sub = tmp_path / "deep" / "nested"
    sub.mkdir(parents=True)
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"gh_mirror_dir": ".max/gh-mirror"}), encoding="utf-8"
    )
    cfg = _mirror.load_config(sub)
    assert cfg.error is None
    assert cfg.path == str(tmp_path / ".max" / "gh-mirror")


def test_malformed_json_is_an_error_not_a_silent_not_configured(tmp_path) -> None:
    (tmp_path / ".supertool.json").write_text("{not json", encoding="utf-8")
    cfg = _mirror.load_config(tmp_path)
    assert cfg.path is None
    assert cfg.error is not None, "a broken config must not read the same as an absent one"


def test_non_string_mirror_dir_is_an_error(tmp_path) -> None:
    (tmp_path / ".supertool.json").write_text(json.dumps({"gh_mirror_dir": 5}), encoding="utf-8")
    cfg = _mirror.load_config(tmp_path)
    assert cfg.path is None
    assert cfg.error is not None


def test_group_writable_config_is_skipped_like_an_absent_one(tmp_path) -> None:
    """Matches every sibling trust check in this codebase (#2366 family):
    an untrusted candidate is skipped, not surfaced as `.error` -- the walk
    keeps going for a further, trusted config higher up."""
    if os.name != "posix":
        pytest.skip("st_uid/write bits are POSIX-only")
    candidate = tmp_path / ".supertool.json"
    candidate.write_text(json.dumps({"gh_mirror_dir": ".max/gh-mirror"}), encoding="utf-8")
    candidate.chmod(0o666)
    try:
        cfg = _mirror.load_config(tmp_path)
        assert cfg.path is None
        assert cfg.error is None, "an untrusted file is a skip, not a reported error"
    finally:
        candidate.chmod(0o644)


# ---------------------------------------------------------------------------
# write_issue / read_issue: cached, not-cached, mirror-unreadable
# ---------------------------------------------------------------------------

_PAYLOAD = {"number": 1955, "title": "mirror", "body": "the full untruncated body"}


def test_a_write_then_a_read_reports_cached_with_an_age(tmp_path) -> None:
    root = str(tmp_path / "mirror")
    err = _mirror.write_issue(root, "1955", _PAYLOAD)
    assert err is None, err

    hit = _mirror.read_issue(root, "1955")

    assert hit.state == _mirror.CACHED
    assert hit.age != "", "a just-written entry must report SOME age, not blank"
    stored = json.loads((Path(root) / "issues" / "1955.json").read_text(encoding="utf-8"))
    assert stored["issue"] == _PAYLOAD, "the mirror must hold the payload untruncated"


def test_an_issue_never_written_is_not_cached(tmp_path) -> None:
    """The positive control for every 'must not serve stale' case below:
    a genuinely empty mirror answers not-cached, proving the harness can
    tell 'nothing happened' from 'something was found'."""
    root = str(tmp_path / "mirror")
    _mirror.write_issue(root, "1", _PAYLOAD)  # something else is cached

    hit = _mirror.read_issue(root, "999")

    assert hit.state == _mirror.NOT_CACHED


def test_a_mirror_root_that_was_never_written_at_all_is_not_cached(tmp_path) -> None:
    root = str(tmp_path / "never-created")
    hit = _mirror.read_issue(root, "1955")
    assert hit.state == _mirror.NOT_CACHED


def test_a_corrupted_manifest_is_mirror_unreadable_never_not_cached(tmp_path) -> None:
    root = tmp_path / "mirror"
    (root / "issues").mkdir(parents=True)
    (root / "issues" / "manifest.json").write_text("{not json at all", encoding="utf-8")

    hit = _mirror.read_issue(str(root), "1955")

    assert hit.state == _mirror.UNREADABLE, hit
    assert hit.state != _mirror.NOT_CACHED
    assert "manifest" in hit.detail.lower() or "corrupted" in hit.detail.lower()


def test_a_manifest_entry_whose_body_file_is_missing_is_mirror_unreadable(tmp_path) -> None:
    """An inconsistent mirror (manifest says cached, body absent) must not
    silently degrade to not-cached -- that would hide real data loss."""
    root = tmp_path / "mirror"
    (root / "issues").mkdir(parents=True)
    (root / "issues" / "manifest.json").write_text(
        json.dumps({"1955": "2026-09-09T00:00:00+00:00"}), encoding="utf-8"
    )
    # No 1955.json written.

    hit = _mirror.read_issue(str(root), "1955")

    assert hit.state == _mirror.UNREADABLE, hit


def test_a_manifest_entry_whose_body_file_is_corrupted_json_is_mirror_unreadable(tmp_path) -> None:
    root = tmp_path / "mirror"
    (root / "issues").mkdir(parents=True)
    (root / "issues" / "manifest.json").write_text(
        json.dumps({"1955": "2026-09-09T00:00:00+00:00"}), encoding="utf-8"
    )
    (root / "issues" / "1955.json").write_text("{not json", encoding="utf-8")

    hit = _mirror.read_issue(str(root), "1955")

    assert hit.state == _mirror.UNREADABLE, hit


def test_permission_denied_on_the_issues_directory_is_mirror_unreadable_never_not_cached(tmp_path) -> None:
    """The defect class this whole module exists to avoid: `Path.is_file()`
    silently swallows `PermissionError` and returns False, which reads
    identically to 'never fetched'. This is the fixture that would pass on
    a `.is_file()`-based implementation and must fail on it."""
    if os.name != "posix":
        pytest.skip("st_uid/write bits are POSIX-only")
    if os.getuid() == 0:
        pytest.skip("root bypasses permission bits by design")
    root = tmp_path / "mirror"
    issues_dir = root / "issues"
    issues_dir.mkdir(parents=True)
    (issues_dir / "manifest.json").write_text(json.dumps({"1955": "x"}), encoding="utf-8")
    issues_dir.chmod(0o000)
    try:
        hit = _mirror.read_issue(str(root), "1955")
        assert hit.state == _mirror.UNREADABLE, hit
        assert hit.state != _mirror.NOT_CACHED
    finally:
        issues_dir.chmod(0o755)


def test_a_manifest_entry_with_an_unparseable_timestamp_is_mirror_unreadable_not_cached(tmp_path) -> None:
    """A non-empty string that is not a valid ISO timestamp passed both the
    'is it None' and 'is it a non-empty string' checks on an earlier cut of
    this code, and once the body file parsed clean, fell all the way through
    to CACHED with a blank age (self-review finding: Explore + oss:auditor
    both caught this independently on #1955's own review). A corrupted
    manifest record must render as `mirror-unreadable`, never as a
    trustworthy cache hit with a blank age."""
    root = tmp_path / "mirror"
    (root / "issues").mkdir(parents=True)
    (root / "issues" / "manifest.json").write_text(
        json.dumps({"1955": "not-a-timestamp"}), encoding="utf-8"
    )
    (root / "issues" / "1955.json").write_text(
        json.dumps({"read_at": "not-a-timestamp", "issue": {"number": 1955}}),
        encoding="utf-8",
    )

    hit = _mirror.read_issue(str(root), "1955")

    assert hit.state == _mirror.UNREADABLE, hit
    assert hit.age == "", "must not report a blank age as though it were a real cache hit"


def test_two_different_issues_do_not_collide(tmp_path) -> None:
    root = str(tmp_path / "mirror")
    _mirror.write_issue(root, "1", {"number": 1, "body": "one"})
    _mirror.write_issue(root, "2", {"number": 2, "body": "two"})

    hit1 = _mirror.read_issue(root, "1")
    hit2 = _mirror.read_issue(root, "2")

    assert hit1.state == _mirror.CACHED
    assert hit2.state == _mirror.CACHED
    body1 = json.loads((Path(root) / "issues" / "1.json").read_text(encoding="utf-8"))
    body2 = json.loads((Path(root) / "issues" / "2.json").read_text(encoding="utf-8"))
    assert body1["issue"]["body"] == "one"
    assert body2["issue"]["body"] == "two"


def test_a_held_manifest_lock_blocks_a_concurrent_write_until_released(tmp_path, monkeypatch) -> None:
    """Self-review finding (#1955, Explore spawn): two concurrent `gh-issue`
    reads for two DIFFERENT numbers could each read the manifest before the
    other wrote it back, so whichever `os.replace` landed second silently
    dropped the other's newly-added key -- exactly the multi-agent scenario
    `_mirror.py`'s own module docstring names as a live concern ("two agents
    claiming the same issue"). This proves the fix's mutual exclusion
    directly: a write that finds the lock already held must wait for it,
    not proceed past it."""
    monkeypatch.setattr(_mirror, "_LOCK_TIMEOUT", 5.0)
    monkeypatch.setattr(_mirror, "_LOCK_POLL", 0.02)
    root = str(tmp_path / "mirror")
    issues_dir = Path(root) / "issues"
    issues_dir.mkdir(parents=True)
    lock_path = issues_dir / (_mirror.MANIFEST_NAME + ".lock")
    lock_path.write_text("held by another process", encoding="utf-8")

    done = threading.Event()

    def run() -> None:
        _mirror.write_issue(root, "1", {"number": 1})
        done.set()

    t = threading.Thread(target=run)
    t.start()
    try:
        blocked_while_locked = not done.wait(timeout=0.3)
        lock_path.unlink()
        finished_after_release = done.wait(timeout=2.0)
    finally:
        t.join(timeout=2)

    assert blocked_while_locked, "write_issue proceeded while the manifest lock was held"
    assert finished_after_release, "write_issue never completed once the lock was released"
    manifest = json.loads((issues_dir / "manifest.json").read_text(encoding="utf-8"))
    assert "1" in manifest


def test_rewriting_an_issue_updates_its_manifest_timestamp(tmp_path, monkeypatch) -> None:
    root = str(tmp_path / "mirror")
    _mirror.write_issue(root, "1955", {"number": 1955, "body": "v1"})
    manifest_path = Path(root) / "issues" / "manifest.json"
    first = json.loads(manifest_path.read_text(encoding="utf-8"))["1955"]

    _mirror.write_issue(root, "1955", {"number": 1955, "body": "v2"})
    second = json.loads(manifest_path.read_text(encoding="utf-8"))["1955"]

    stored = json.loads((Path(root) / "issues" / "1955.json").read_text(encoding="utf-8"))
    assert stored["issue"]["body"] == "v2", "the mirror must hold the latest read, not the first"
    assert second >= first
