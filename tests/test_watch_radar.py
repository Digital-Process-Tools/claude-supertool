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
    env["monkeypatch"].setattr(radar, "live_open_mrs", lambda multi=None: mrs_list)


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
    def _boom(multi=None):
        raise radar.RadarError("glab not authenticated. Run: glab auth login")

    env["monkeypatch"].setattr(radar, "live_open_mrs", _boom)
    assert radar.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "glab not authenticated" in captured.err


def test_glab_failure_does_not_prune_heal_or_snapshot(env, capsys) -> None:
    merged = _state_file(env["dir"], "33136", mr_state="merged")

    def _boom(multi=None):
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
    Path(radar._snapshot_path(radar.default_filter())).write_text("{ not json")
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
    def _boom(multi=None):
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


# ---------------------------------------------------------------------------
# the filter arg (issue #425)
#
# These stub `glab mr list` rather than `live_open_mrs`, because the whole
# point of the issue is that the *board* is built from the filter. A test that
# stubs live_open_mrs cannot tell a working radar from one that reads the
# filter for its watcher fleet and keeps a hardcoded author for its board.
# ---------------------------------------------------------------------------

class _Result:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


def _glab(env, by_author: dict[str, list[dict]]) -> list[list[str]]:
    """Serve `glab mr list` from a per-author map. Returns the argv log."""
    calls: list[list[str]] = []

    def _run_cmd(cmd, timeout=25):
        calls.append(list(cmd))
        author = cmd[cmd.index("--author") + 1] if "--author" in cmd else ""
        return _Result(json.dumps(by_author.get(author, [])))

    env["monkeypatch"].setattr(radar.mrs, "_run", _run_cmd)
    env["monkeypatch"].setattr(radar.mrs, "_enrich", lambda data, cap, workers: None)
    return calls


def _authors(calls: list[list[str]]) -> list[str]:
    return [c[c.index("--author") + 1] for c in calls if "--author" in c]


def _feed_ids(env) -> list[str]:
    return [wid for _src, wid, _only in _feed_spawns(env)]


def test_a_filter_arg_moves_the_board_the_fleet_and_the_feed_together(env, capsys) -> None:
    """One filter, one population. The invariant, pinned on all three views.

    Half-implementing this — filter the fleet, keep `author=@me` for the board
    — produces a session receiving events for MRs the board does not list,
    which renders exactly like a board where those MRs are fine.
    """
    _glab(env, {"@me": [_mr(33161)], "modular.system": [_mr(991, "failed", "800")]})

    assert radar.main(["radar", "author=modular.system"]) == 0
    out = capsys.readouterr().out

    assert "!991" in out
    assert "!33161" not in out
    assert _mr_spawns(env) == ["991"]
    assert _feed_ids(env) == ["author=modular.system"]


def test_a_bare_radar_still_covers_my_own_open_mrs(env, capsys) -> None:
    _glab(env, {"@me": [_mr(33161)], "modular.system": [_mr(991)]})
    assert radar.main([]) == 0
    out = capsys.readouterr().out
    assert "!33161" in out
    assert "!991" not in out


def test_a_bare_radar_keeps_the_feed_alias_rather_than_forking_a_second_poller(env, capsys) -> None:
    """The feed id is a filename. `@me` and its expansion are one population,
    so they must not become two pollers emitting two copies of every event."""
    _glab(env, {"@me": [_mr(33161)]})
    assert radar.main([]) == 0
    assert _feed_ids(env) == ["@me"]


def test_a_filter_matching_a_feed_alias_reuses_that_alias(env, capsys) -> None:
    _glab(env, {"": [_mr(4242)]})
    assert radar.main(["radar", "reviewer=@me,state=opened"]) == 0
    assert "!4242" in capsys.readouterr().out
    assert _feed_ids(env) == ["@reviewer"]


def test_an_arg_carrying_only_flags_is_still_the_default_population(env, capsys) -> None:
    """`radar:nopipe` names no filter. Resolving it to an empty dict would mint
    an empty feed scope — a third poller over the population `@me` already
    covers, and therefore duplicate discovery events."""
    _glab(env, {"@me": [_mr(33161)]})
    assert radar.main(["radar", "nopipe"]) == 0
    out = capsys.readouterr().out
    assert "!33161" in out
    assert _feed_ids(env) == ["@me"]
    assert radar.resolve_filter(["radar", "nopipe"]) == radar.default_filter()


def test_editing_the_shared_default_moves_radar_too(env, capsys) -> None:
    """The drift hole this issue names: `DEFAULT_FEED` used to move only the
    shell supervisor, leaving radar on its own hardcoded author."""
    calls = _glab(env, {"someone.else": [_mr(77)]})
    env["monkeypatch"].setattr(radar.defaults, "DEFAULT_FILTER",
                               "author=someone.else,state=opened")
    assert radar.main([]) == 0
    assert "!77" in capsys.readouterr().out
    assert _authors(calls) == ["someone.else"]


# ---------------------------------------------------------------------------
# multi-author fan-out
# ---------------------------------------------------------------------------

def test_two_authors_become_two_queries_unioned_by_iid(env, capsys) -> None:
    calls = _glab(env, {"@me": [_mr(33161), _mr(500)],
                        "modular.system": [_mr(991), _mr(500)]})

    assert radar.main(["radar", "author=@me,author=modular.system"]) == 0
    out = capsys.readouterr().out

    assert _authors(calls) == ["@me", "modular.system"]
    assert sorted(_mr_spawns(env)) == ["33161", "500", "991"]
    assert "3 open" in out


def test_an_mr_both_authors_return_is_listed_once(env, capsys) -> None:
    _glab(env, {"@me": [_mr(500)], "modular.system": [_mr(500)]})
    assert radar.main(["radar", "author=@me,author=modular.system"]) == 0
    out = capsys.readouterr().out
    assert out.count("!500") == 1
    assert "1 open" in out


def test_a_failing_query_in_the_fanout_fails_the_whole_board(env, capsys) -> None:
    """A partial union is a board quietly missing rows — the exact failure
    RadarError exists to prevent, so one bad query is not survivable."""
    def _run_cmd(cmd, timeout=25):
        author = cmd[cmd.index("--author") + 1] if "--author" in cmd else ""
        if author == "modular.system":
            return _Result("", returncode=1)
        return _Result(json.dumps([_mr(33161)]))

    env["monkeypatch"].setattr(radar.mrs, "_run", _run_cmd)
    assert radar.main(["radar", "author=@me,author=modular.system"]) == 1
    assert capsys.readouterr().out == ""


def test_a_non_default_board_names_its_population(env, capsys) -> None:
    """Two radars in one window otherwise print two indistinguishable boards."""
    _glab(env, {"modular.system": [_mr(991)]})
    assert radar.main(["radar", "author=modular.system"]) == 0
    assert "author=modular.system" in capsys.readouterr().out


def test_the_default_board_is_not_labelled(env, capsys) -> None:
    _glab(env, {"@me": [_mr(33161)]})
    assert radar.main([]) == 0
    assert "author=@me" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# snapshot keying — two filters, two deltas
# ---------------------------------------------------------------------------

def test_each_filter_gets_its_own_snapshot_file() -> None:
    mine = radar._snapshot_path(radar.default_filter())
    kevin = radar._snapshot_path(radar.resolve_filter(["radar", "author=modular.system"]))
    assert mine != kevin


def test_filter_key_ignores_order_but_not_identity() -> None:
    both_ways = [radar.filter_key(radar.resolve_filter(["radar", arg]))
                 for arg in ("author=a,author=b", "author=b,author=a")]
    assert both_ways[0] == both_ways[1]
    assert radar.filter_key(radar.resolve_filter(["radar", "author=a"])) != both_ways[0]


def test_a_second_population_does_not_read_as_everything_new_and_everything_gone(
        env, capsys) -> None:
    """Sharing one snapshot file makes the delta column lie in both directions:
    every row of one population is new, every row of the other is gone."""
    _glab(env, {"@me": [_mr(33161)], "modular.system": [_mr(991)]})

    assert radar.main([]) == 0
    capsys.readouterr()

    assert radar.main(["radar", "author=modular.system"]) == 0
    kevin = capsys.readouterr().out
    assert "cold start" in kevin
    assert "no longer open" not in kevin

    assert radar.main([]) == 0
    mine = capsys.readouterr().out
    assert "radar: no change" in mine
    assert "no longer open" not in mine


def test_a_filtered_run_writes_only_its_own_snapshot(env, capsys) -> None:
    _glab(env, {"@me": [_mr(33161)], "modular.system": [_mr(991)]})
    kevin_filter = radar.resolve_filter(["radar", "author=modular.system"])

    assert radar.main(["radar", "author=modular.system"]) == 0
    capsys.readouterr()

    assert radar.read_snapshot() is None
    assert set(radar.read_snapshot(kevin_filter)["mrs"]) == {"991"}


# ---------------------------------------------------------------------------
# standing exclusions — suppressing a row must never suppress the accounting
#
# An exclusion is the one feature in this op that can hide a red, so every
# test here asks the same question from a different side: what does the board
# still say about the MR it stopped showing? A suppression that leaves no
# trace is the `## No error patterns matched` defect with a config file.
# ---------------------------------------------------------------------------

BLOG_TITLE = "the max blog, permanently conflicted"
BLOG_REASON = "MySQL service TLS failure + standing conflict, not this MR"


def _exclude(env, mapping) -> None:
    env["monkeypatch"].setenv(radar.EXCLUSIONS_ENV, json.dumps(mapping))


def test_a_configured_exclusion_keeps_that_mr_off_the_board(env, capsys) -> None:
    """The row, not the MR: the whole point is that a permanently-red row
    stops training the reader to skim."""
    _exclude(env, {"19509": {"reason": BLOG_REASON}})
    _set_live(env, [_mr(19509, "failed", "154177", title=BLOG_TITLE),
                    _mr(19510, "failed", "154200", title="a real red")])
    out = _run(env, capsys)

    assert BLOG_TITLE not in out
    assert "a real red" in out


def test_an_excluded_mr_is_still_accounted_for_in_the_output(env, capsys) -> None:
    """The pin that stops this feature becoming a silent-omission bug.

    Asserted on the emitted text, not on an internal count: a footer that
    knows about the exclusion while the board says nothing is exactly the
    blind spot the exclusion was supposed to be an honest alternative to.
    """
    _exclude(env, {"19509": {"reason": BLOG_REASON}})
    _set_live(env, [_mr(19509, "failed", "154177", title=BLOG_TITLE),
                    _mr(19510, "success", "154200", title="a real green")])
    out = _run(env, capsys)

    assert "1 excluded" in out
    assert "!19509" in out
    assert BLOG_REASON in out
    assert "failed" in out


def test_the_footer_counts_describe_the_board_that_was_printed(env, capsys) -> None:
    """`3 open | 1 failing` with only two rows and no red among them is a
    dangling reference — the reader hunts for a row that was never printed.
    The tallies cover the shown board; `N excluded` restores the total."""
    _exclude(env, {"19509": {"reason": BLOG_REASON}})
    _set_live(env, [_mr(19509, "failed", "154177", title=BLOG_TITLE),
                    _mr(19510, "success", "154200", title="a real green")])
    out = _run(env, capsys)
    footer = [ln for ln in out.splitlines() if " open" in ln][0]

    assert "1 open" in footer
    assert "failing" not in footer
    assert "1 green" in footer
    assert "1 excluded" in footer


def test_an_exclusion_does_not_match_a_longer_iid(env, capsys) -> None:
    """Exact iid, never a prefix: excluding 1950 must not swallow 19509."""
    _exclude(env, {"1950": {"reason": "some other branch"}})
    _set_live(env, [_mr(19509, "failed", "154177", title=BLOG_TITLE)])
    out = _run(env, capsys)

    assert BLOG_TITLE in out
    assert "excluded !19509" not in out


def test_an_excluded_mr_is_still_watched_healed_and_fully_notified(env, capsys) -> None:
    """Scope decision, pinned: an exclusion is a statement about one row, not
    a change of population. Dropping it from the fleet would stop the watcher
    that reports the moment the exclusion stops being true — and the event is
    the only thing that can report a push while the row is suppressed."""
    _exclude(env, {"19509": {"reason": BLOG_REASON}})
    _set_live(env, [_mr(19509, "failed", "154177", title=BLOG_TITLE)])
    out = _run(env, capsys)

    assert _mr_spawns(env) == ["19509"]
    only = [o for src, iid, o in env["spawned"] if iid == "19509"][0]
    assert only == [e for e in radar.defaults.DEFAULT_ONLY.split(",") if e]
    assert "still watched" in out


def test_an_exclusion_never_narrows_the_feed(env, capsys) -> None:
    """The feed is the discovery tier for a *population*. An excluded MR is
    still in the population, so the feed scope is untouched."""
    _exclude(env, {"19509": {"reason": BLOG_REASON}})
    _set_live(env, [_mr(19509, "failed", "154177", title=BLOG_TITLE)])
    _run(env, capsys)

    assert [scope for _s, scope, _o in _feed_spawns(env)] == [radar.FEED_SCOPE]


def test_an_excluded_mr_is_still_recorded_in_the_snapshot(env, capsys) -> None:
    """Otherwise removing the exclusion reports a four-month-old MR as new."""
    _exclude(env, {"19509": {"reason": BLOG_REASON}})
    _set_live(env, [_mr(19509, "failed", "154177", title=BLOG_TITLE)])
    _run(env, capsys)

    assert "19509" in radar.read_snapshot()["mrs"]


def test_an_exclusion_stops_applying_once_the_mr_is_no_longer_red(env, capsys) -> None:
    """Self-expiry. An exclusion suppresses a *standing problem*; it can never
    hide an MR that is fine, so the reason cannot outlive itself unnoticed."""
    _exclude(env, {"19509": {"reason": BLOG_REASON}})
    _set_live(env, [_mr(19509, "success", "154177", title=BLOG_TITLE)])
    out = _run(env, capsys)

    assert BLOG_TITLE in out
    assert "NOT applied" in out
    assert "1 excluded" not in out


def test_an_expired_exclusion_is_not_applied(env, capsys) -> None:
    _exclude(env, {"19509": {"reason": BLOG_REASON, "until": "2020-01-01"}})
    _set_live(env, [_mr(19509, "failed", "154177", title=BLOG_TITLE)])
    out = _run(env, capsys)

    assert BLOG_TITLE in out
    assert "NOT applied" in out
    assert "2020-01-01" in out


def test_an_unexpired_exclusion_is_applied(env, capsys) -> None:
    _exclude(env, {"19509": {"reason": BLOG_REASON, "until": "2999-01-01"}})
    _set_live(env, [_mr(19509, "failed", "154177", title=BLOG_TITLE)])
    out = _run(env, capsys)

    assert BLOG_TITLE not in out
    assert "1 excluded" in out


def test_an_exclusion_for_an_mr_outside_the_population_says_so(env, capsys) -> None:
    """Dead config is the quiet half of staleness: it suppresses nothing today
    and everything on the day that iid is reused or reopened."""
    _exclude(env, {"19509": {"reason": BLOG_REASON}})
    _set_live(env, [_mr(19510, "success", "154200", title="a real green")])
    out = _run(env, capsys)

    assert "NOT applied" in out
    assert "!19509" in out


def test_an_exclusion_without_a_reason_is_refused(env, capsys) -> None:
    """Fail open, loudly. An unreasoned exclusion is the thing that becomes a
    permanent blind spot, so it is not honoured and the row renders."""
    _exclude(env, {"19509": {}})
    _set_live(env, [_mr(19509, "failed", "154177", title=BLOG_TITLE)])
    out = _run(env, capsys)

    assert BLOG_TITLE in out
    assert "no reason" in out


def test_a_malformed_exclusions_config_fails_open(env, capsys) -> None:
    """Every unanswerable case resolves to *show the row*."""
    env["monkeypatch"].setenv(radar.EXCLUSIONS_ENV, "{not json")
    _set_live(env, [_mr(19509, "failed", "154177", title=BLOG_TITLE)])
    out = _run(env, capsys)

    assert BLOG_TITLE in out


def test_the_exclusion_note_survives_a_no_change_run(env, capsys) -> None:
    """The delta is what made the row repetitive; it must not also be what
    makes the suppression invisible. A board whose only red is excluded still
    says so on every run."""
    _exclude(env, {"19509": {"reason": BLOG_REASON}})
    _set_live(env, [_mr(19509, "failed", "154177", title=BLOG_TITLE)])
    _run(env, capsys)
    out = _run(env, capsys)

    assert "radar: no change" in out
    assert "!19509" in out
    assert BLOG_REASON in out


def test_an_excluded_mr_that_lost_its_watcher_is_reported_as_unwatched(env, capsys) -> None:
    """The exclusion claims coverage. When there is none, it says so rather
    than repeating a comfort it cannot back up."""
    _exclude(env, {"19509": {"reason": BLOG_REASON}})
    _set_live(env, [_mr(19509, "failed", "154177", title=BLOG_TITLE)])
    env["monkeypatch"].setattr(radar.dispatcher, "_spawn_poller",
                               lambda source, wid, only: 0)
    out = _run(env, capsys)

    assert "unwatched" in out
    assert "still watched" not in out


def test_a_conflict_only_red_is_excludable(env, capsys) -> None:
    """!19509's conflict is not a pipeline failure, and the standing-problem
    rule is what re-prints it every run."""
    _exclude(env, {"19509": {"reason": BLOG_REASON}})
    _set_live(env, [_mr(19509, "success", "154177", title=BLOG_TITLE,
                        has_conflicts=True)])
    out = _run(env, capsys)

    assert BLOG_TITLE not in out
    assert "conflict" in out


def test_an_exclusion_may_be_written_as_a_bare_reason_string(env, capsys) -> None:
    _exclude(env, {"19509": BLOG_REASON})
    _set_live(env, [_mr(19509, "failed", "154177", title=BLOG_TITLE)])
    out = _run(env, capsys)

    assert BLOG_TITLE not in out
    assert BLOG_REASON in out


# ---------------------------------------------------------------------------
# the op wiring — without {args} the whole feature is unreachable
# ---------------------------------------------------------------------------

def test_the_exclusions_env_var_matches_the_documented_config_key() -> None:
    """`ops.radar.radar_exclusions` reaches the preset as
    SUPERTOOL_RADAR_EXCLUSIONS — the op runner uppercases the key and prefixes
    it. A rename on either side is a config key that silently does nothing,
    which is the one failure mode a suppression feature cannot afford."""
    key = "radar_exclusions"
    assert radar.EXCLUSIONS_ENV == f"SUPERTOOL_{key.upper()}"

    docs = (Path(__file__).parent.parent / "docs" / "presets" / "watch.md").read_text()
    assert f'"{key}"' in docs
    assert radar.EXCLUSIONS_ENV in docs

    manifest = json.loads((Path(__file__).parent.parent / "presets" / "watch.json").read_text())
    assert key in manifest["ops"]["radar"]["description"]


def test_the_radar_op_forwards_its_args() -> None:
    manifest = json.loads((Path(__file__).parent.parent / "presets" / "watch.json").read_text())
    op = manifest["ops"]["radar"]
    assert op["cmd"].endswith("{args}")
    assert "author=" in op["syntax"]
