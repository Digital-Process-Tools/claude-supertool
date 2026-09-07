"""Unit tests for presets/gitlab/_maintenance.py — the maintenance-window
lookup #645 wires into gl-job:fail. No filesystem/config walk here; each
function is exercised directly with an in-memory config dict."""
from __future__ import annotations

import importlib.util
import json
import os
from datetime import time
from pathlib import Path

import pytest

PRESET_PATH = Path(__file__).parent.parent / "presets" / "gitlab" / "_maintenance.py"
_spec = importlib.util.spec_from_file_location("gitlab_maintenance", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
maint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(maint)


FLEET_CFG = {
    "gl-runners": {
        "maintenance": {"window": "00:00 UTC", "duration": "5m"},
        "runners": {
            "dptools-runner-4": {"maintenance": {"window": "03:30 UTC", "duration": "5m"}},
            "hercule": {"maintenance": None},
            "dptools-runner-7": {"notes": "decommissioned"},
        },
    }
}


def test_resolve_window_falls_back_to_fleet_default_for_unknown_runner():
    assert maint.resolve_window(FLEET_CFG, "dptools-runner-1", 1) == {
        "window": "00:00 UTC", "duration": "5m"}


def test_resolve_window_per_runner_override_wins():
    assert maint.resolve_window(FLEET_CFG, "dptools-runner-4", 4) == {
        "window": "03:30 UTC", "duration": "5m"}


def test_resolve_window_per_runner_override_merges_missing_keys_from_fleet_default():
    # Self-review finding: a `window`-only override (the docs' own canonical
    # example) must still carry the fleet's `duration`, not silently become
    # a 0-second window nothing can ever fall inside.
    cfg = {
        "gl-runners": {
            "maintenance": {"window": "00:00 UTC", "duration": "5m"},
            "runners": {"dptools-runner-4": {"maintenance": {"window": "03:30 UTC"}}},
        }
    }
    assert maint.resolve_window(cfg, "dptools-runner-4", 4) == {
        "window": "03:30 UTC", "duration": "5m"}
    assert maint.parse_window(maint.resolve_window(cfg, "dptools-runner-4", 4)) == (
        time(3, 30), 300)


def test_resolve_window_explicit_null_opts_out_rather_than_falling_back():
    assert maint.resolve_window(FLEET_CFG, "hercule", 9) is None


def test_resolve_window_entry_without_maintenance_key_falls_back_to_fleet_default():
    # dptools-runner-7 has an entry (notes) but no "maintenance" key at all —
    # that must fall through to the fleet default, not read as an opt-out.
    assert maint.resolve_window(FLEET_CFG, "dptools-runner-7", 7) == {
        "window": "00:00 UTC", "duration": "5m"}


def test_resolve_window_no_gl_runners_config_at_all():
    assert maint.resolve_window({}, "hercule", 9) is None


# --- parse_window: well-formed (positive control) and malformed (#645's own requirement) ---

def test_parse_window_well_formed():
    assert maint.parse_window({"window": "00:00 UTC", "duration": "5m"}) == (time(0, 0), 300)


def test_parse_window_well_formed_hours_unit():
    assert maint.parse_window({"window": "3:30 UTC", "duration": "1h"}) == (time(3, 30), 3600)


def test_parse_window_malformed_marker_reads_as_no_window_not_an_error():
    # The exact "tolerant per-runner marker" requirement from the brief: a
    # garbled declaration is "no window declared", never a raised exception.
    assert maint.parse_window({"window": "not-a-time", "duration": "5m"}) is None
    assert maint.parse_window({"window": "00:00 UTC", "duration": "5 minutes"}) is None
    assert maint.parse_window({"window": "25:00 UTC", "duration": "5m"}) is None
    assert maint.parse_window({"window": "00:99 UTC", "duration": "5m"}) is None
    assert maint.parse_window("00:00 UTC") is None  # wrong shape entirely
    assert maint.parse_window(None) is None
    assert maint.parse_window({}) is None


def test_parse_window_rejects_a_smuggled_trailing_newline():
    # #1188 -- Python's $ matches before a final newline, so a fully-anchored
    # ^...$ pattern with a trailing \s* run (which itself consumes \n) used to
    # accept "00:00 UTC\n" and "5m\n" as if they were the plain, well-formed
    # value. Positive control: the plain value with no trailing newline still
    # parses (test_parse_window_well_formed above already covers that).
    assert maint.parse_window({"window": "00:00 UTC\n", "duration": "5m"}) is None
    assert maint.parse_window({"window": "00:00 UTC", "duration": "5m\n"}) is None
    assert maint.parse_window({"window": "00:00 UTC\n", "duration": "5m\n"}) is None


def test_parse_window_never_raises_on_wrong_types():
    assert maint.parse_window({"window": 123, "duration": "5m"}) is None
    assert maint.parse_window({"window": "00:00 UTC", "duration": 5}) is None


# --- died_in_window: three states ---

def test_died_in_window_true_inside():
    assert maint.died_in_window("2026-07-31T00:00:08Z", time(0, 0), 300) is True


def test_died_in_window_false_outside():
    assert maint.died_in_window("2026-07-31T00:12:31Z", time(0, 0), 300) is False


def test_died_in_window_none_when_finished_at_unparseable():
    assert maint.died_in_window("not-a-timestamp", time(0, 0), 300) is None
    assert maint.died_in_window(None, time(0, 0), 300) is None


def test_died_in_window_wraps_past_midnight():
    # window starting 23:58 for 5 minutes wraps to 00:03 the next day.
    assert maint.died_in_window("2026-07-31T23:59:00Z", time(23, 58), 300) is True
    assert maint.died_in_window("2026-08-01T00:01:00Z", time(23, 58), 300) is True
    assert maint.died_in_window("2026-07-31T12:00:00Z", time(23, 58), 300) is False


# --- is_container_exit / exit_code_from_texts: scope guard ---

def test_is_container_exit_true_for_137_139_143():
    assert maint.is_container_exit(137)
    assert maint.is_container_exit(139)
    assert maint.is_container_exit(143)


def test_is_container_exit_false_for_a_real_test_failure_code():
    # #645's scope guard: a PHPUnit failure (exit 1) must never qualify.
    assert not maint.is_container_exit(1)
    assert not maint.is_container_exit(None)


def test_exit_code_from_texts_extracts_the_number():
    assert maint.exit_code_from_texts(["ERROR: Job failed: exit code 137"]) == 137


def test_exit_code_from_texts_none_when_absent():
    assert maint.exit_code_from_texts(["some other boilerplate line"]) is None


# --- maintenance_note: the full block, wired via load_config monkeypatch ---

def test_maintenance_note_empty_for_non_container_exit(monkeypatch):
    monkeypatch.setattr(maint, "load_config", lambda: FLEET_CFG)
    assert maint.maintenance_note(1, "2026-07-31T00:00:08Z", None, None) == ""


def test_maintenance_note_empty_when_no_window_declared(monkeypatch):
    monkeypatch.setattr(maint, "load_config", lambda: {})
    assert maint.maintenance_note(137, "2026-07-31T00:00:08Z", None, None) == ""


def test_maintenance_note_in_window_says_retry(monkeypatch):
    monkeypatch.setattr(maint, "load_config", lambda: FLEET_CFG)
    note = maint.maintenance_note(137, "2026-07-31T00:00:08Z", "dptools-runner-1", 1)
    assert "inside the declared maintenance window" in note
    assert "RETRY" in note


def test_maintenance_note_outside_window_names_it_but_does_not_clear_it(monkeypatch):
    monkeypatch.setattr(maint, "load_config", lambda: FLEET_CFG)
    note = maint.maintenance_note(137, "2026-07-31T06:00:00Z", "dptools-runner-1", 1)
    assert "outside the declared maintenance window" in note
    assert "RETRY" not in note


# --- load_config: the #695 trust-boundary hardening, reproduced for this
# preset-local loader (self-review finding: this file's own walk used to go
# all the way to filesystem root with no ownership/permission check, unlike
# the hardened `_supertool._load_config`). ---

def _fresh_load_config_module():
    """A second import of the module, so `_CACHED_CONFIG` starts unset --
    the real `maint` import at module scope is already cached by other
    tests in this file by the time these run."""
    spec = importlib.util.spec_from_file_location(
        "gitlab_maintenance_loadconfig", PRESET_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_load_config_stops_at_git_ancestor_rather_than_reaching_further_up(
    tmp_path, monkeypatch,
):
    # outer/  (.supertool.json here -- must NOT be picked up)
    #   repo/  (.git here -- the boundary)
    #     sub/  (cwd -- no .supertool.json here)
    outer = tmp_path / "outer"
    repo = outer / "repo"
    sub = repo / "sub"
    sub.mkdir(parents=True)
    (repo / ".git").mkdir()
    (outer / ".supertool.json").write_text(
        json.dumps({"gl-runners": {"maintenance": {"window": "09:00 UTC", "duration": "5m"}}}),
        encoding="utf-8",
    )
    monkeypatch.chdir(sub)
    mod = _fresh_load_config_module()
    assert mod.load_config() == {}


@pytest.mark.skipif(os.name != "posix", reason="ownership/permission bits are POSIX-only")
def test_load_config_skips_a_group_world_writable_file(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    candidate = tmp_path / ".supertool.json"
    candidate.write_text(
        json.dumps({"gl-runners": {"maintenance": {"window": "09:00 UTC", "duration": "5m"}}}),
        encoding="utf-8",
    )
    candidate.chmod(0o666)  # world-writable
    monkeypatch.chdir(tmp_path)
    mod = _fresh_load_config_module()
    try:
        assert mod.load_config() == {}
    finally:
        candidate.chmod(0o644)


def test_load_config_reads_an_ordinary_config_normally(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    candidate = tmp_path / ".supertool.json"
    payload = {"gl-runners": {"maintenance": {"window": "09:00 UTC", "duration": "5m"}}}
    candidate.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    mod = _fresh_load_config_module()
    assert mod.load_config() == payload


def test_maintenance_note_unreadable_finish_time_says_could_not_tell(monkeypatch):
    monkeypatch.setattr(maint, "load_config", lambda: FLEET_CFG)
    note = maint.maintenance_note(137, "garbage", "dptools-runner-1", 1)
    assert "could not tell" in note


def test_maintenance_note_malformed_window_is_named_not_collapsed_into_silence(monkeypatch):
    # A declared-but-broken window is a third state, distinct from both a
    # real verdict and "nothing configured" -- it must never render as the
    # same empty string as test_maintenance_note_empty_when_no_window_declared.
    broken_cfg = {"gl-runners": {"maintenance": {"window": "midnight-ish", "duration": "5m"}}}
    monkeypatch.setattr(maint, "load_config", lambda: broken_cfg)
    note = maint.maintenance_note(137, "2026-07-31T00:00:08Z", "dptools-runner-1", 1)
    assert note != ""
    assert "could not be parsed" in note
    assert "RETRY" not in note
