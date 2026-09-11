"""`_statusline_fragments` — the atomic, worktree-keyed cache statusline reads (#1850).

TDD: written before `presets/_statusline_fragments.py` existed, so importing
it is itself the first RED. Three properties are pinned:

* **atomic** — a reader never sees a half-written fragment.
* **worktree-keyed** — two different directories never collide on one slot.
* **three states on read** — `not-published` (never run this session, the
  normal case) must never render the same as `unreadable` (a fragment exists
  but could not be trusted). Collapsing the two is exactly the "absence
  produced by the tool, read as an absence in the world" defect class this
  repository keeps re-filing.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "statusline_fragments_1850", REPO / "presets" / "_statusline_fragments.py")
assert _spec is not None and _spec.loader is not None
frag = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(frag)


def test_never_run_this_session_is_not_published(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPERTOOL_STATUSLINE_CACHE_DIR", str(tmp_path / "cache"))
    data, state = frag.read("gh-pr", str(tmp_path / "worktree"))
    assert state == "not-published"
    assert data is None


def test_a_published_fragment_reads_back_ok(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPERTOOL_STATUSLINE_CACHE_DIR", str(tmp_path / "cache"))
    wt = tmp_path / "worktree"
    wt.mkdir()
    path = frag.publish("gh-pr", str(wt), {"summary": "3 total: 3 passed, 0 failed, 0 pending"})
    assert path is not None
    data, state = frag.read("gh-pr", str(wt))
    assert state == "ok"
    assert data["summary"] == "3 total: 3 passed, 0 failed, 0 pending"
    assert isinstance(data["_ts"], float)


def test_a_corrupt_fragment_is_unreadable_not_not_published(monkeypatch, tmp_path):
    """The positive control for the negative assertion below: a real absence
    (no file at all) must read differently from a present-but-broken one."""
    cache = tmp_path / "cache"
    monkeypatch.setenv("SUPERTOOL_STATUSLINE_CACHE_DIR", str(cache))
    wt = tmp_path / "worktree"
    wt.mkdir()
    # Publish once so the path/key exist, then corrupt the bytes on disk.
    frag.publish("gh-pr", str(wt), {"summary": "ok"})
    key = frag._key(str(wt))
    target = cache / f"{key}.gh-pr.json"
    target.write_text("{not json", encoding="utf-8")

    data, state = frag.read("gh-pr", str(wt))
    assert state == "unreadable"
    assert data is None

    # Positive control: a genuinely absent op in the SAME worktree still
    # reports the normal absence, proving the harness can tell the two apart.
    data2, state2 = frag.read("radar", str(wt))
    assert state2 == "not-published"


def test_two_worktrees_never_share_a_slot(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPERTOOL_STATUSLINE_CACHE_DIR", str(tmp_path / "cache"))
    wt_a = tmp_path / "a"
    wt_b = tmp_path / "b"
    wt_a.mkdir()
    wt_b.mkdir()
    frag.publish("gh-pr", str(wt_a), {"summary": "A's tally"})

    data_a, state_a = frag.read("gh-pr", str(wt_a))
    data_b, state_b = frag.read("gh-pr", str(wt_b))

    assert state_a == "ok" and data_a["summary"] == "A's tally"
    assert state_b == "not-published"


def test_publish_is_atomic_no_partial_file_left_behind(monkeypatch, tmp_path):
    cache = tmp_path / "cache"
    monkeypatch.setenv("SUPERTOOL_STATUSLINE_CACHE_DIR", str(cache))
    wt = tmp_path / "worktree"
    wt.mkdir()
    frag.publish("gh-pr", str(wt), {"summary": "ok"})
    # No leftover temp file after a normal publish -- os.replace() is atomic
    # and the fixed target path is the only artefact.
    leftovers = [p for p in cache.iterdir() if p.name.startswith(".tmp-")]
    assert leftovers == []


def test_publish_never_raises_when_the_cache_dir_cannot_be_made(monkeypatch, tmp_path):
    """Best-effort: a cache-write failure must never propagate into the
    caller (`gh-pr` here) and turn a working read into a failing one."""
    # Point the cache dir at a path that can never become a directory: a file.
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")
    monkeypatch.setenv("SUPERTOOL_STATUSLINE_CACHE_DIR", str(blocker / "cache"))
    result = frag.publish("gh-pr", str(tmp_path), {"summary": "ok"})
    assert result is None


def test_publish_from_a_subdirectory_is_readable_from_the_worktree_root(monkeypatch, tmp_path):
    """Self-review finding (Explore reviewer, #1850): `gh-pr` publishes under
    whatever `os.getcwd()` happens to be, while `statusline` reads under
    Claude Code's reported `workspace.current_dir` -- typically the project
    root. Those two must resolve to the SAME slot when both point somewhere
    inside one git worktree, even when the exact subdirectory differs (a
    monorepo subpackage, a wrapper script that `cd`s before invoking
    supertool), or a fragment that genuinely exists reads as `not-published`.
    """
    monkeypatch.setenv("SUPERTOOL_STATUSLINE_CACHE_DIR", str(tmp_path / "cache"))
    repo = tmp_path / "repo"
    sub = repo / "packages" / "sub"
    sub.mkdir(parents=True)
    (repo / ".git").mkdir()  # enough to mark the worktree root -- no real git needed

    frag.publish("gh-pr", str(sub), {"summary": "3 total: 3 passed, 0 failed, 0 pending"})

    data, state = frag.read("gh-pr", str(repo))
    assert state == "ok", (data, state)
    assert data["summary"] == "3 total: 3 passed, 0 failed, 0 pending"


def test_two_worktrees_with_no_git_marker_still_never_collide(monkeypatch, tmp_path):
    """Positive control for the fix above: two plain directories with no
    `.git` anywhere up their tree (the shape every OTHER existing test in
    this file uses) must still key independently -- the worktree-root
    resolution must never widen the key to something coarser than intended
    when there is no repository to find."""
    monkeypatch.setenv("SUPERTOOL_STATUSLINE_CACHE_DIR", str(tmp_path / "cache"))
    wt_a = tmp_path / "a"
    wt_b = tmp_path / "b"
    wt_a.mkdir()
    wt_b.mkdir()
    frag.publish("gh-pr", str(wt_a), {"summary": "A's tally"})

    data_b, state_b = frag.read("gh-pr", str(wt_b))
    assert state_b == "not-published"


def test_age_seconds_is_none_without_a_timestamp():
    assert frag.age_seconds({"summary": "ok"}) is None


def test_age_seconds_reads_a_recent_publish_as_near_zero(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPERTOOL_STATUSLINE_CACHE_DIR", str(tmp_path / "cache"))
    wt = tmp_path / "worktree"
    wt.mkdir()
    frag.publish("gh-pr", str(wt), {"summary": "ok"})
    data, _state = frag.read("gh-pr", str(wt))
    age = frag.age_seconds(data)
    assert age is not None
    assert 0.0 <= age < 5.0
