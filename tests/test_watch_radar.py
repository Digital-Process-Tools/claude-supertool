"""Tests for the `radar` op (issue #417 item 5).

radar is a reconcile, not a printer: live GitLab is authoritative, the state
files are cache. These tests cover the reconcile — pruning, drift, and the
healing respawn — not just the render, and every fixture that carries both a
`last_event` and a `source_state` gives them *different* pipeline ids, since
believing `last_event` is the bug the op exists to prevent.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

WATCH_DIR = Path(__file__).parent.parent / "presets" / "watch"

_spec = importlib.util.spec_from_file_location("watch_radar", WATCH_DIR / "radar.py")
assert _spec is not None and _spec.loader is not None
radar = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(radar)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _mr(iid, pipeline="success", pipeline_id="100", title="a title",
        updated="2026-07-27T10:00:00Z", failed_jobs=None, **kw) -> dict:
    m = {
        "iid": int(iid),
        "title": title,
        "source_branch": f"max/branch-{iid}",
        "target_branch": "master",
        "updated_at": updated,
        "_pipeline": pipeline,
        "_pipeline_id": pipeline_id,
        "_pipeline_url": "",
        "_changes": 3,
        "_approved": True,
        "_approved_by": [],
        "_failed_jobs": failed_jobs or ([] if pipeline != "failed" else ["test_unit"]),
    }
    m.update(kw)
    return m


def _state_file(tmp_path: Path, iid: str, *, mr_state="opened",
                source_pipeline_id="100", event_pipeline_id=None,
                event="pipeline_failed", pipeline_status="running") -> Path:
    """A watcher state file. event_pipeline_id defaults to a *stale* id."""
    body: dict = {
        "source_state": {
            "mr_state": mr_state,
            "pipeline_status": pipeline_status,
            "pipeline_id": source_pipeline_id,
        },
    }
    if event_pipeline_id is not None:
        body["last_event"] = {
            "ts": "2026-07-27T09:00:00Z",
            "event": event,
            "payload": {"pipeline_id": event_pipeline_id},
        }
    path = tmp_path / f"supertool-watch-gitlab-mr__{iid}.state.json"
    path.write_text(json.dumps(body))
    return path


def _live_pid_file(tmp_path: Path, iid: str) -> Path:
    path = tmp_path / f"supertool-watch-gitlab-mr__{iid}.pid"
    path.write_text(f"{os.getpid()}\n")
    return path


def _dead_pid_file(tmp_path: Path, iid: str) -> Path:
    path = tmp_path / f"supertool-watch-gitlab-mr__{iid}.pid"
    path.write_text("9999999\n")
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Redirect all radar state into tmp_path and record every respawn."""
    monkeypatch.setattr(radar.transport, "STATE_DIR", str(tmp_path))
    spawned: list[tuple[str, str, list[str]]] = []

    def _fake_spawn(source, watcher_id, only):
        spawned.append((source, watcher_id, list(only)))
        path = tmp_path / f"supertool-watch-{source}__{watcher_id}.pid"
        path.write_text(f"{os.getpid()}\n")
        return 4242

    monkeypatch.setattr(radar.dispatcher, "_spawn_poller", _fake_spawn)
    return {"dir": tmp_path, "spawned": spawned, "monkeypatch": monkeypatch}


def _mr_spawns(env) -> list[str]:
    """iids of per-MR watchers radar spawned — the feed is a separate source."""
    return [iid for src, iid, _o in env["spawned"] if src == radar.SOURCE]


def _feed_spawns(env) -> list[tuple[str, str, list[str]]]:
    return [row for row in env["spawned"] if row[0] == radar.FEED_SOURCE]


def _live_feed(tmp_path: Path) -> Path:
    path = tmp_path / f"supertool-watch-{radar.FEED_SOURCE}__{radar.FEED_SCOPE}.pid"
    path.write_text(f"{os.getpid()}\n")
    return path


def _set_live(env, mrs_list):
    env["monkeypatch"].setattr(radar, "live_open_mrs", lambda: mrs_list)


def _run(env, capsys) -> str:
    rc = radar.main([])
    assert rc == 0
    return capsys.readouterr().out


# ---------------------------------------------------------------------------
# cold start — no state files at all
# ---------------------------------------------------------------------------

def test_cold_start_renders_full_board_from_live_gitlab(env, capsys) -> None:
    _set_live(env, [_mr(33161, "failed", "154177"), _mr(33172, "success", "154200")])
    out = _run(env, capsys)
    assert "cold start" in out
    assert "!33161" in out
    assert "!33172" in out


def test_cold_start_spawns_a_watcher_for_every_open_mr(env, capsys) -> None:
    _set_live(env, [_mr(33161, "failed"), _mr(33172, "success"), _mr(33173, "running")])
    _run(env, capsys)
    assert sorted(_mr_spawns(env)) == ["33161", "33172", "33173"]


def test_healed_watchers_use_the_shared_default_event_filter(env, capsys) -> None:
    _set_live(env, [_mr(33161)])
    _run(env, capsys)
    source, iid, only = env["spawned"][0]
    assert source == "gitlab-mr"
    assert iid == "33161"
    assert only == [
        "pipeline_failed", "pipeline_succeeded", "merged", "closed", "conflicts_appeared",
    ]


def test_cold_start_with_no_open_mrs_says_so(env, capsys) -> None:
    _set_live(env, [])
    out = _run(env, capsys)
    assert "No open MRs." in out
    assert _mr_spawns(env) == []


# ---------------------------------------------------------------------------
# heal — the step that makes coverage survive a reboot
# ---------------------------------------------------------------------------

def test_open_mr_with_dead_poller_is_healed(env, capsys) -> None:
    _dead_pid_file(env["dir"], "33161")
    _set_live(env, [_mr(33161, "running")])
    out = _run(env, capsys)
    assert _mr_spawns(env) == ["33161"]
    assert "[healed]" in out
    assert "1 healed" in out


def test_open_mr_with_live_poller_is_not_respawned(env, capsys) -> None:
    _live_pid_file(env["dir"], "33161")
    _live_feed(env["dir"])
    _set_live(env, [_mr(33161, "running")])
    out = _run(env, capsys)
    assert env["spawned"] == []
    assert "[healed]" not in out
    assert "👁" in out


def test_failed_respawn_is_reported_as_a_coverage_gap(env, capsys) -> None:
    env["monkeypatch"].setattr(radar.dispatcher, "_spawn_poller", lambda *a: 0)
    _set_live(env, [_mr(33161, "running")])
    out = _run(env, capsys)
    assert "[unwatched]" in out
    assert "1 unwatched" in out


# ---------------------------------------------------------------------------
# prune — item 4
# ---------------------------------------------------------------------------

def test_terminal_state_file_is_pruned(env, capsys) -> None:
    merged = _state_file(env["dir"], "33136", mr_state="merged")
    _set_live(env, [_mr(33161)])
    out = _run(env, capsys)
    assert not merged.exists()
    assert "1 pruned" in out


def test_non_terminal_state_file_survives(env, capsys) -> None:
    opened = _state_file(env["dir"], "33161", mr_state="opened")
    _set_live(env, [_mr(33161)])
    _run(env, capsys)
    assert opened.exists()


def test_state_file_owned_by_a_live_poller_is_not_pruned(env, capsys) -> None:
    """A live poller owns its file and clears it itself when it stops."""
    _live_pid_file(env["dir"], "33136")
    still = _state_file(env["dir"], "33136", mr_state="merged")
    _set_live(env, [_mr(33161)])
    _run(env, capsys)
    assert still.exists()


# ---------------------------------------------------------------------------
# drift — last_event vs source_state
# ---------------------------------------------------------------------------

def test_drift_reports_both_pipeline_ids(env, capsys) -> None:
    _live_pid_file(env["dir"], "33173")
    _state_file(env["dir"], "33173", source_pipeline_id="154180",
                event_pipeline_id="154177")
    _set_live(env, [_mr(33173, "running", "154180")])
    out = _run(env, capsys)
    assert "[drift: 154177→154180]" in out
    assert "1 drift" in out


def test_no_drift_when_event_and_source_agree(env, capsys) -> None:
    _live_pid_file(env["dir"], "33173")
    _state_file(env["dir"], "33173", source_pipeline_id="154180",
                event_pipeline_id="154180")
    _set_live(env, [_mr(33173, "running", "154180")])
    out = _run(env, capsys)
    assert "drift" not in out


def test_pruned_state_file_does_not_report_drift(env, capsys) -> None:
    _state_file(env["dir"], "33136", mr_state="merged",
                source_pipeline_id="154180", event_pipeline_id="154177")
    _set_live(env, [_mr(33161)])
    out = _run(env, capsys)
    assert "drift" not in out


# ---------------------------------------------------------------------------
# source_state / live truth beats last_event
# ---------------------------------------------------------------------------

def test_status_comes_from_live_state_not_last_event(env, capsys) -> None:
    """!33173 live: last_event says failed on 154177, truth is running on 154180."""
    _live_pid_file(env["dir"], "33173")
    _state_file(env["dir"], "33173", pipeline_status="running",
                source_pipeline_id="154180", event="pipeline_failed",
                event_pipeline_id="154177")
    _set_live(env, [_mr(33173, "running", "154180", title="Generator loadable")])
    out = _run(env, capsys)
    row = next(ln for ln in out.splitlines() if "!33173" in ln)
    assert "running" in row
    assert "✗" not in row
    assert "0 failing" not in out


def test_stale_green_state_file_does_not_suppress_a_live_red_mr(env, capsys) -> None:
    """The cold-start failure: cache says green, GitLab says red. Red wins."""
    _live_pid_file(env["dir"], "33161")
    _state_file(env["dir"], "33161", pipeline_status="success",
                source_pipeline_id="100", event="pipeline_succeeded",
                event_pipeline_id="100")
    _set_live(env, [_mr(33161, "failed", "999", failed_jobs=["test_unit_dpt"])])
    out = _run(env, capsys)
    row = next(ln for ln in out.splitlines() if "!33161" in ln)
    assert "test_unit_dpt" in row
    assert "1 failing" in out


def test_absent_state_files_do_not_suppress_a_live_red_mr(env, capsys) -> None:
    _set_live(env, [_mr(33161, "failed", "999", failed_jobs=["phpstan2"])])
    out = _run(env, capsys)
    assert "phpstan2" in out
    assert "1 failing" in out


# ---------------------------------------------------------------------------
# delta-only reporting
# ---------------------------------------------------------------------------

def test_second_run_with_nothing_moved_is_one_summary_line(env, capsys) -> None:
    _live_pid_file(env["dir"], "33172")
    _live_feed(env["dir"])
    _set_live(env, [_mr(33172, "success", "100")])
    _run(env, capsys)
    out = _run(env, capsys)
    assert out == "radar: no change | 1 open | 1 green | 1 watched | feed ok\n"


def test_nothing_moved_still_reports_coverage_rather_than_silence(env, capsys) -> None:
    """Total silence is indistinguishable from a radar that never ran."""
    _live_pid_file(env["dir"], "33172")
    _set_live(env, [_mr(33172, "success")])
    _run(env, capsys)
    assert _run(env, capsys).strip() != ""


def test_pipeline_going_red_breaks_the_silence(env, capsys) -> None:
    _live_pid_file(env["dir"], "33172")
    _set_live(env, [_mr(33172, "success", "100")])
    _run(env, capsys)
    _set_live(env, [_mr(33172, "failed", "101", failed_jobs=["rector"])])
    out = _run(env, capsys)
    assert "!33172" in out
    assert "rector" in out


def test_standing_failure_is_reprinted_even_when_unchanged(env, capsys) -> None:
    """An unfixed red is a current fact, not history — never delta-suppressed."""
    _live_pid_file(env["dir"], "33161")
    _set_live(env, [_mr(33161, "failed", "154177", failed_jobs=["phpstan2"])])
    _run(env, capsys)
    out = _run(env, capsys)
    assert "!33161" in out
    assert "phpstan2" in out


def test_unchanged_green_mr_is_suppressed_next_to_a_standing_red(env, capsys) -> None:
    _live_pid_file(env["dir"], "33161")
    _live_pid_file(env["dir"], "33172")
    _set_live(env, [
        _mr(33161, "failed", "154177", failed_jobs=["phpstan2"]),
        _mr(33172, "success", "100"),
    ])
    _run(env, capsys)
    out = _run(env, capsys)
    assert "!33161" in out
    assert "!33172" not in out


def test_new_open_mr_appears_on_the_next_run(env, capsys) -> None:
    _live_pid_file(env["dir"], "33172")
    _set_live(env, [_mr(33172, "success")])
    _run(env, capsys)
    _live_pid_file(env["dir"], "33199")
    _set_live(env, [_mr(33172, "success"), _mr(33199, "success")])
    out = _run(env, capsys)
    assert "!33199" in out
    assert "!33172" not in out


def test_merged_mr_is_counted_as_no_longer_open(env, capsys) -> None:
    _live_pid_file(env["dir"], "33172")
    _live_pid_file(env["dir"], "33136")
    _set_live(env, [_mr(33172, "success"), _mr(33136, "success")])
    _run(env, capsys)
    _set_live(env, [_mr(33172, "success")])
    out = _run(env, capsys)
    assert "1 no longer open" in out


# ---------------------------------------------------------------------------
# render helpers
# ---------------------------------------------------------------------------

def test_rows_use_the_shared_gl_mrs_row_format(env, capsys) -> None:
    """radar must not invent a second board layout — both lines of it."""
    _live_pid_file(env["dir"], "33161")
    m = _mr(33161, "failed", "154177", failed_jobs=["phpstan2"])
    _set_live(env, [m])
    out = _run(env, capsys)
    expected = radar.mrs._row(m, {"33161"}, True)
    assert "\n" in expected, "a row is two lines: status, then the full title"
    assert expected in out


def test_drift_and_gap_marks_are_appended_not_substituted(env, capsys) -> None:
    _dead_pid_file(env["dir"], "33173")
    _state_file(env["dir"], "33173", source_pipeline_id="154180",
                event_pipeline_id="154177")
    m = _mr(33173, "running", "154180")
    _set_live(env, [m])
    out = _run(env, capsys)
    marks = "  [drift: 154177→154180] [healed]"
    assert radar.mrs._row(m, {"33173"}, True, marks) in out


def test_drift_and_gap_marks_land_on_the_status_line_not_the_title(env, capsys) -> None:
    """Marks annotate pipeline state, so they belong beside the status. Trailing
    the prose title they read as part of the sentence."""
    _dead_pid_file(env["dir"], "33173")
    _state_file(env["dir"], "33173", source_pipeline_id="154180",
                event_pipeline_id="154177")
    _set_live(env, [_mr(33173, "running", "154180", title="A readable title")])
    lines = _run(env, capsys).splitlines()
    idx = next(i for i, ln in enumerate(lines) if "!33173" in ln)
    assert lines[idx].endswith("[drift: 154177→154180] [healed]")
    assert lines[idx + 1] == "        A readable title"


# ---------------------------------------------------------------------------
# legibility — a human reads this board (#421)
# ---------------------------------------------------------------------------

def test_long_title_is_rendered_in_full_not_truncated(env, capsys) -> None:
    """The reported bug: titles were cut at 42 chars, mid-word, exactly where
    the disambiguating detail lives ('...loadable and cov')."""
    title = "Make the Generator module loadable and coverable"
    assert len(title) > 42, "fixture must exceed the old truncation budget"
    _live_pid_file(env["dir"], "33173")
    _set_live(env, [_mr(33173, "failed", "154177", title=title)])
    out = _run(env, capsys)
    assert title in out
    assert "loadable and cov\n" not in out


def test_row_shows_source_and_target_branch(env, capsys) -> None:
    """The branch is what a human acts on. Without it every actionable row cost
    a follow-up gl-mr:<iid>:status round-trip just to locate the code."""
    _live_pid_file(env["dir"], "33173")
    _set_live(env, [_mr(33173, "failed", "154177",
                        source_branch="max/generator-testability-coverage",
                        target_branch="master")])
    out = _run(env, capsys)
    assert "max/generator-testability-coverage -> master" in out


def test_branch_shares_the_actionable_status_line_with_the_iid(env, capsys) -> None:
    _live_pid_file(env["dir"], "33173")
    _set_live(env, [_mr(33173, "failed", "154177", title="Some prose title",
                        source_branch="max/foo", target_branch="v18.9")])
    status = next(ln for ln in _run(env, capsys).splitlines() if "!33173" in ln)
    assert "max/foo -> v18.9" in status
    assert "Some prose title" not in status


def test_board_costs_no_extra_api_call_for_the_branch(env) -> None:
    """source_branch/target_branch ship in the single glab mr list response, so
    legibility must not have bought itself a per-MR round-trip."""
    calls: list[list[str]] = []
    payload = ('[{"iid": 33173, "title": "t", '
               '"source_branch": "max/foo", "target_branch": "master"}]')
    env["monkeypatch"].setattr(
        radar.mrs, "_run",
        lambda cmd, timeout=25: (calls.append(cmd), _completed(0, payload))[1])
    env["monkeypatch"].setattr(radar.mrs, "_enrich", lambda *a, **k: None)
    live = radar.live_open_mrs()
    assert len(calls) == 1
    assert radar.mrs._branches(live[0]) == "max/foo -> master"


# ---------------------------------------------------------------------------
# live truth is mandatory — never degrade to "all green"
# ---------------------------------------------------------------------------

def test_glab_failure_exits_nonzero_and_prints_no_board(env, capsys) -> None:
    def _boom():
        raise radar.RadarError("glab not authenticated. Run: glab auth login")

    env["monkeypatch"].setattr(radar, "live_open_mrs", _boom)
    assert radar.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "glab not authenticated" in captured.err


def test_glab_failure_does_not_prune_heal_or_snapshot(env, capsys) -> None:
    merged = _state_file(env["dir"], "33136", mr_state="merged")

    def _boom():
        raise radar.RadarError("boom")

    env["monkeypatch"].setattr(radar, "live_open_mrs", _boom)
    radar.main([])
    assert merged.exists()
    assert env["spawned"] == []
    assert radar.read_snapshot() is None


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["glab"], returncode, stdout, stderr)


def test_live_open_mrs_raises_on_glab_error(env) -> None:
    env["monkeypatch"].setattr(radar.mrs, "_run",
                               lambda *a, **k: _completed(1, "", "401 Unauthorized"))
    with pytest.raises(radar.RadarError, match="not authenticated"):
        radar.live_open_mrs()


def test_live_open_mrs_raises_on_bad_json(env) -> None:
    env["monkeypatch"].setattr(radar.mrs, "_run", lambda *a, **k: _completed(0, "not json"))
    with pytest.raises(radar.RadarError, match="parse"):
        radar.live_open_mrs()


def test_live_open_mrs_queries_open_mrs_authored_by_me(env) -> None:
    seen: list[list[str]] = []

    def _capture(cmd, timeout=25):
        seen.append(cmd)
        return _completed(0, "[]")

    env["monkeypatch"].setattr(radar.mrs, "_run", _capture)
    env["monkeypatch"].setattr(radar.mrs, "_enrich", lambda *a, **k: None)
    assert radar.live_open_mrs() == []
    assert seen[0][:4] == ["glab", "mr", "list", "-F"]
    assert "--author" in seen[0] and "@me" in seen[0]
    assert "--merged" not in seen[0] and "--all" not in seen[0]


def test_live_truth_is_one_gl_mrs_query(env) -> None:
    calls: list[list[str]] = []
    env["monkeypatch"].setattr(radar.mrs, "_run",
                               lambda cmd, timeout=25: (calls.append(cmd), _completed(0, "[]"))[1])
    env["monkeypatch"].setattr(radar.mrs, "_enrich", lambda *a, **k: None)
    radar.live_open_mrs()
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# snapshot handling
# ---------------------------------------------------------------------------

def test_corrupt_snapshot_is_treated_as_cold_start(env, capsys) -> None:
    Path(radar._snapshot_path()).write_text("{ not json")
    _live_pid_file(env["dir"], "33172")
    _set_live(env, [_mr(33172, "success")])
    assert "cold start" in _run(env, capsys)


# ---------------------------------------------------------------------------
# feed — reconcile is a snapshot, the feed is what keeps discovering (#422)
# ---------------------------------------------------------------------------

def _feed_state(tmp_path: Path, body: dict) -> Path:
    path = (tmp_path /
            f"supertool-watch-{radar.FEED_SOURCE}__{radar.FEED_SCOPE}.state.json")
    path.write_text(json.dumps(body))
    return path


def test_radar_starts_the_feed_poller_with_the_shared_feed_defaults(env, capsys) -> None:
    _set_live(env, [_mr(33161)])
    _run(env, capsys)
    assert _feed_spawns(env) == [
        ("gitlab-mr-feed", "@me",
         ["mr_opened", "mr_merged", "mr_closed", "mr_left_feed"]),
    ]


def test_a_second_radar_run_does_not_start_a_second_feed_poller(env, capsys) -> None:
    """Idempotence is the whole point of radar; n feeds means n mr_opened."""
    _set_live(env, [_mr(33161)])
    _run(env, capsys)
    _run(env, capsys)
    _run(env, capsys)
    assert len(_feed_spawns(env)) == 1


def test_a_feed_poller_already_alive_is_left_alone(env, capsys) -> None:
    _live_feed(env["dir"])
    _set_live(env, [_mr(33161)])
    out = _run(env, capsys)
    assert _feed_spawns(env) == []
    assert "feed ok" in out


def test_a_dead_feed_poller_is_respawned_and_the_respawn_is_reported(env, capsys) -> None:
    (env["dir"] /
     f"supertool-watch-{radar.FEED_SOURCE}__{radar.FEED_SCOPE}.pid").write_text("9999999\n")
    _set_live(env, [_mr(33161)])
    out = _run(env, capsys)
    assert len(_feed_spawns(env)) == 1
    assert "feed respawned" in out


def test_a_feed_that_cannot_be_started_is_reported_not_silently_absent(env, capsys) -> None:
    """A missing feed and a quiet day render identically unless radar says so."""
    real = radar.dispatcher._spawn_poller
    env["monkeypatch"].setattr(
        radar.dispatcher, "_spawn_poller",
        lambda source, wid, only: 0 if source == radar.FEED_SOURCE else real(source, wid, only))
    _set_live(env, [_mr(33161)])
    out = _run(env, capsys)
    assert "WARNING" in out
    assert "will not be discovered" in out
    assert "feed DOWN" in out


def test_a_down_feed_still_warns_on_a_run_where_nothing_moved(env, capsys) -> None:
    """Delta suppression must not swallow the report that discovery is off."""
    real = radar.dispatcher._spawn_poller
    env["monkeypatch"].setattr(
        radar.dispatcher, "_spawn_poller",
        lambda source, wid, only: 0 if source == radar.FEED_SOURCE else real(source, wid, only))
    _live_pid_file(env["dir"], "33172")
    _set_live(env, [_mr(33172, "success", "100")])
    _run(env, capsys)
    out = _run(env, capsys)
    assert "WARNING" in out
    assert "radar: no change" in out


def test_a_feed_alive_but_failing_every_poll_is_reported(env, capsys) -> None:
    """`watches` shows it green. Discovery is still dead, so radar says so."""
    _live_feed(env["dir"])
    _feed_state(env["dir"], {"last_error": {"ts": "2026-07-27T16:00:00Z",
                                            "message": "glab: 401 Unauthorized"}})
    _set_live(env, [_mr(33161)])
    out = _run(env, capsys)
    assert "WARNING" in out
    assert "glab: 401 Unauthorized" in out


def test_a_healthy_feed_produces_no_warning(env, capsys) -> None:
    _live_feed(env["dir"])
    _feed_state(env["dir"], {"source_state": {"known": {"33161": {}}}})
    _set_live(env, [_mr(33161)])
    out = _run(env, capsys)
    assert "WARNING" not in out


def test_unreachable_gitlab_does_not_start_a_feed_poller(env, capsys) -> None:
    """The hard-error path takes no action at all, feed included."""
    def _boom():
        raise radar.RadarError("boom")

    env["monkeypatch"].setattr(radar, "live_open_mrs", _boom)
    assert radar.main([]) == 1
    assert _feed_spawns(env) == []


def test_snapshot_records_the_reported_facts(env, capsys) -> None:
    _live_pid_file(env["dir"], "33161")
    _set_live(env, [_mr(33161, "failed", "154177")])
    _run(env, capsys)
    snap = radar.read_snapshot()
    assert snap is not None
    assert snap["mrs"]["33161"]["pipeline"] == "failed"
    assert snap["mrs"]["33161"]["pipeline_id"] == "154177"
