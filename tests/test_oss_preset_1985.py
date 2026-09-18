"""presets/oss -- a shim to the installed `claude-oss` plugin (#1985).

Two things under test, loaded standalone (`importlib`-free `sys.path`
insert, same convention as `test_watch_foreign_poller_version_2529.py`):
`shim.resolve` never fabricates a version when the install record cannot
answer, and `tick.compose` always fills every row -- three states, never a
silent skip that reads like a clean pass.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from _changelog_findable import assert_change_is_findable

OSS_DIR = Path(__file__).parent.parent / "presets" / "oss"
sys.path.insert(0, str(OSS_DIR))
import shim  # noqa: E402
import tick  # noqa: E402


def test_change_is_findable():
    assert_change_is_findable(1985)


# --- shim.resolve --------------------------------------------------------


def _write_record(tmp_path, plugins):
    record = tmp_path / "installed_plugins.json"
    record.write_text(json.dumps({"plugins": plugins}), encoding="utf-8")
    return record


def test_resolve_could_not_resolve_when_not_in_record(tmp_path):
    record = _write_record(tmp_path, {})
    state, detail = shim.resolve(record=record)
    assert state == "could-not-resolve"
    assert "not in the install record" in detail


def test_resolve_could_not_resolve_when_record_unreadable(tmp_path):
    state, detail = shim.resolve(record=tmp_path / "missing.json")
    assert state == "could-not-resolve"


def test_resolve_could_not_resolve_when_install_path_absent(tmp_path):
    record = _write_record(
        tmp_path,
        {"oss@marketplace": [{"version": "0.40.0"}]},
    )
    # No installPath, and the cache glob (default ~/.claude/plugins/cache)
    # will not resolve to this tmp_path -- point cache_root somewhere empty.
    state, detail = shim.resolve(
        record=record, cache_root=tmp_path / "empty-cache"
    )
    assert state == "could-not-resolve"
    assert "0.40.0" in detail


def test_resolve_resolved_but_different_when_no_scripts_dir(tmp_path):
    install_path = tmp_path / "install"
    install_path.mkdir()
    record = _write_record(
        tmp_path,
        {
            "oss@marketplace": [
                {"version": "0.40.0", "installPath": str(install_path)}
            ]
        },
    )
    state, detail = shim.resolve(record=record)
    assert state == "resolved-but-different"
    assert "no scripts/" in detail


def test_resolve_ok_via_install_path(tmp_path):
    install_path = tmp_path / "install"
    (install_path / "scripts").mkdir(parents=True)
    record = _write_record(
        tmp_path,
        {
            "oss@marketplace": [
                {"version": "0.40.0", "installPath": str(install_path)}
            ]
        },
    )
    state, detail = shim.resolve(record=record)
    assert state == "resolved"
    version, scripts_dir = detail
    assert version == "0.40.0"
    assert scripts_dir == install_path / "scripts"


def test_resolve_falls_back_to_cache_glob_without_install_path(tmp_path):
    cache_root = tmp_path / "cache"
    scripts = cache_root / "dpt-plugins" / "oss" / "0.40.0" / "scripts"
    scripts.mkdir(parents=True)
    record = _write_record(
        tmp_path, {"oss@marketplace": [{"version": "0.40.0"}]}
    )
    state, detail = shim.resolve(record=record, cache_root=cache_root)
    assert state == "resolved"
    version, scripts_dir = detail
    assert version == "0.40.0"
    assert scripts_dir == scripts


def test_resolve_picks_highest_of_multiple_scopes(tmp_path):
    cache_root = tmp_path / "cache"
    for version in ("0.30.0", "0.40.0"):
        (cache_root / "dpt-plugins" / "oss" / version / "scripts").mkdir(
            parents=True
        )
    record = _write_record(
        tmp_path,
        {
            "oss@marketplace": [
                {"version": "0.30.0"},
                {"version": "0.40.0"},
            ]
        },
    )
    state, detail = shim.resolve(record=record, cache_root=cache_root)
    assert state == "resolved"
    version, _ = detail
    assert version == "0.40.0"


# --- tick.compose ----------------------------------------------------------


class _FakeCompleted:
    def __init__(self, returncode, stdout):
        self.returncode = returncode
        self.stdout = stdout


def _fake_run_factory(git_ok=True, board_ok=True, radar="registered"):
    def _fake_run(argv, **kwargs):
        joined = " ".join(str(a) for a in argv)
        if "git" in argv and "fetch" in argv:
            return _FakeCompleted(0 if git_ok else 1, b"" if git_ok else b"fetch failed")
        if "git" in argv and "pull" in argv:
            return _FakeCompleted(
                0 if git_ok else 1,
                b"Already up to date." if git_ok else b"pull failed",
            )
        if "oss_state.py" in joined:
            if "--last" in argv:
                return _FakeCompleted(0, b"2026-09-01T00:00:00Z decision=dispatch")
            if "--pending-wait" in argv:
                return _FakeCompleted(0, b"cleared")
            if "--check-plugin-identity" in argv:
                return _FakeCompleted(0, b"unchanged")
            return _FakeCompleted(1, b"unhandled oss_state.py call")
        if "radar:--state" in argv:
            if radar == "not-configured":
                return _FakeCompleted(1, b"radar: not configured")
            return _FakeCompleted(0 if radar == "registered" else 1, b"registered")
        # board ops: gh-prs, gh-issues, gh-branch, git-worktrees
        return _FakeCompleted(0 if board_ok else 1, b"" if board_ok else b"failed")

    return _fake_run


def _resolved(tmp_path):
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "oss_state.py").write_text("#!/usr/bin/env python3\n")

    def _resolve_fn(record=None, cache_root=None):
        return "resolved", ("0.40.0", scripts_dir)

    return _resolve_fn


def test_compose_every_row_present_on_the_happy_path(tmp_path):
    (tmp_path / ".oss.local.json").write_text(
        json.dumps({"clone": str(tmp_path), "state_file": ".max/oss-watch.json"}),
        encoding="utf-8",
    )
    (tmp_path / ".max").mkdir()
    (tmp_path / ".max" / "oss-watch.json").write_text("{}", encoding="utf-8")

    rows = tick.compose(
        cwd=str(tmp_path),
        run=_fake_run_factory(),
        resolve_fn=_resolved(tmp_path),
    )
    assert rows["plugin_identity"] == "resolved 0.40.0"
    assert "dispatch" in rows["last_state_entry"]
    assert rows["pending_wait"] == "cleared"
    assert rows["plugin_identity_check"] == "unchanged"
    assert rows["git_sync"] == "Already up to date."
    assert all(v == "read" for v in rows["board"].values())
    assert rows["radar_tier"] == "registered"
    assert rows["next"] == "proceed to dispatch"


def test_compose_reports_could_not_resolve_never_silently(tmp_path):
    def _unresolved(record=None, cache_root=None):
        return "could-not-resolve", "oss is not in the install record"

    rows = tick.compose(
        cwd=str(tmp_path), run=_fake_run_factory(), resolve_fn=_unresolved
    )
    assert rows["plugin_identity"].startswith("could-not-resolve")
    # A skipped step and a step that found nothing must never render alike --
    # the three state-file rows must NOT silently claim success.
    assert rows["last_state_entry"].startswith("FAIL")
    assert rows["pending_wait"].startswith("could-not-evaluate")
    assert rows["plugin_identity_check"].startswith("could-not-tell")
    assert rows["next"].startswith("resolve the oss plugin install")


def test_compose_reports_could_not_evaluate_without_local_config(tmp_path):
    """No `.oss.local.json` (a worktree cut from a clone that has one) is a
    real 'could not evaluate', never a crash and never a false pass."""
    rows = tick.compose(
        cwd=str(tmp_path), run=_fake_run_factory(), resolve_fn=_resolved(tmp_path)
    )
    assert rows["last_state_entry"].startswith("FAIL")
    assert "state file" in rows["last_state_entry"]
    assert rows["pending_wait"].startswith("could-not-evaluate")


def test_compose_git_sync_failure_surfaces_and_drives_next(tmp_path):
    (tmp_path / ".oss.local.json").write_text(
        json.dumps({"clone": str(tmp_path), "state_file": ".max/oss-watch.json"}),
        encoding="utf-8",
    )
    (tmp_path / ".max").mkdir()
    (tmp_path / ".max" / "oss-watch.json").write_text("{}", encoding="utf-8")

    rows = tick.compose(
        cwd=str(tmp_path),
        run=_fake_run_factory(git_ok=False),
        resolve_fn=_resolved(tmp_path),
    )
    assert rows["git_sync"].startswith("could-not-run")
    assert rows["next"].startswith("resolve the git sync failure")


def test_compose_unread_board_member_surfaces_and_drives_next(tmp_path):
    (tmp_path / ".oss.local.json").write_text(
        json.dumps({"clone": str(tmp_path), "state_file": ".max/oss-watch.json"}),
        encoding="utf-8",
    )
    (tmp_path / ".max").mkdir()
    (tmp_path / ".max" / "oss-watch.json").write_text("{}", encoding="utf-8")

    rows = tick.compose(
        cwd=str(tmp_path),
        run=_fake_run_factory(board_ok=False),
        resolve_fn=_resolved(tmp_path),
    )
    assert all(v.startswith("unread") for v in rows["board"].values())
    assert rows["next"].startswith("re-read the board")


def test_compose_radar_not_configured_is_a_state_not_a_failure(tmp_path):
    (tmp_path / ".oss.local.json").write_text(
        json.dumps({"clone": str(tmp_path), "state_file": ".max/oss-watch.json"}),
        encoding="utf-8",
    )
    (tmp_path / ".max").mkdir()
    (tmp_path / ".max" / "oss-watch.json").write_text("{}", encoding="utf-8")

    rows = tick.compose(
        cwd=str(tmp_path),
        run=_fake_run_factory(radar="not-configured"),
        resolve_fn=_resolved(tmp_path),
    )
    assert rows["radar_tier"] == "not-configured"


def test_render_includes_every_row():
    rows = {
        "plugin_identity": "resolved 0.40.0",
        "last_state_entry": "no entries yet",
        "pending_wait": "cleared",
        "plugin_identity_check": "unchanged",
        "git_sync": "up to date",
        "board": {op: "read" for op in tick._BOARD_OPS},
        "radar_tier": "registered",
        "next": "proceed to dispatch",
    }
    text = tick.render(rows)
    for expected in (
        "resolved 0.40.0", "no entries yet", "cleared", "unchanged",
        "up to date", "registered", "proceed to dispatch",
    ):
        assert expected in text


def test_state_file_missing_local_config_is_could_not_evaluate_not_a_crash(tmp_path):
    path, reason = tick.state_file(str(tmp_path))
    assert path is None
    assert "not found" in reason


def test_state_file_resolves_relative_to_declared_clone(tmp_path):
    clone = tmp_path / "clone"
    (clone / ".max").mkdir(parents=True)
    (clone / ".max" / "oss-watch.json").write_text("{}", encoding="utf-8")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".oss.local.json").write_text(
        json.dumps({"clone": str(clone), "state_file": ".max/oss-watch.json"}),
        encoding="utf-8",
    )
    path, reason = tick.state_file(str(worktree))
    assert reason is None
    assert path == str((clone / ".max" / "oss-watch.json").resolve())
