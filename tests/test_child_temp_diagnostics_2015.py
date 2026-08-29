"""`tests/_child_temp_diagnostics.py` (#2015).

Five Windows CI occurrences of a nested pytest child dying at collection
share nothing but a `FileNotFoundError` naming a temp directory -- the log
shows the crash, not the state that produced it, and a fifth occurrence
(canonical `C:\\Users\\...` spelling) already ruled out the leading
canonicalisation theory. `snapshot_temp_state()` is the instrument this
issue asks for instead of a guessed fix: it must never raise (it is meant
to run from inside a failure path), and it must report the fields a future
occurrence needs rather than silently omitting one.
"""
from __future__ import annotations

import os
import tempfile

from _child_temp_diagnostics import describe, snapshot_temp_state


def test_snapshot_reports_the_real_system_tempdir():
    snap = snapshot_temp_state()
    assert snap["tempdir"] == tempfile.gettempdir()
    assert snap["tempdir_exists"] is True


def test_snapshot_realpath_matches_os_path_realpath_of_a_symlinked_alias(tmp_path):
    """The contract this whole issue turns on: whatever primitive this
    module uses for canonicalisation must actually resolve a symlink/
    junction alias, not merely echo the raw path back."""
    real_target = tmp_path / "real"
    real_target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real_target, target_is_directory=True)

    assert os.path.realpath(str(alias)) == os.path.realpath(str(real_target))
    assert str(alias) != os.path.realpath(str(alias))


def test_snapshot_reports_xdg_cache_home_when_set(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    snap = snapshot_temp_state()
    assert snap["XDG_CACHE_HOME"] == str(tmp_path)
    assert snap["XDG_CACHE_HOME_exists"] is True


def test_snapshot_reports_none_for_a_missing_xdg_cache_home(monkeypatch):
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    snap = snapshot_temp_state()
    assert snap["XDG_CACHE_HOME"] is None
    assert snap["XDG_CACHE_HOME_exists"] is None


def test_snapshot_reports_false_when_xdg_cache_home_points_nowhere(monkeypatch, tmp_path):
    """The must-fire half of the pair above: a set-but-gone directory must
    read as False, not as the same None a never-set variable produces --
    those are different facts, and this issue is precisely about losing the
    distinction between "never existed" and "existed, then vanished"."""
    gone = tmp_path / "does-not-exist"
    monkeypatch.setenv("XDG_CACHE_HOME", str(gone))
    snap = snapshot_temp_state()
    assert snap["XDG_CACHE_HOME"] == str(gone)
    assert snap["XDG_CACHE_HOME_exists"] is False


def test_snapshot_never_raises_even_when_realpath_itself_fails(monkeypatch):
    def _boom(_path):
        raise OSError("simulated WinError 5")

    monkeypatch.setattr(os.path, "realpath", _boom)
    snap = snapshot_temp_state()
    assert "simulated WinError 5" in snap["tempdir_realpath"]


def test_snapshot_never_raises_even_when_isdir_itself_fails(monkeypatch):
    def _boom(_path):
        raise OSError("simulated WinError 5")

    monkeypatch.setattr(os.path, "isdir", _boom)
    snap = snapshot_temp_state()
    assert "simulated WinError 5" in snap["tempdir_exists"]


def test_describe_renders_the_label_and_every_field():
    snap = {"a": 1, "b": None}
    text = describe("before", snap)
    assert "before" in text
    assert "a: 1" in text
    assert "b: None" in text
