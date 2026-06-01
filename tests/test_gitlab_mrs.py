"""Tests for the gl-mrs triage-board op.

Covers the pure helpers — arg parsing, glab-cmd construction, watch-state
cross-reference, pipeline glyph mapping, table render, and footer hint —
without hitting the live glab CLI.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

PRESET_PATH = Path(__file__).parent.parent / "presets" / "gitlab" / "mrs.py"
_spec = importlib.util.spec_from_file_location("gitlab_mrs", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
mrs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mrs)


# ---------------------------------------------------------------------------
# _parse_args
# ---------------------------------------------------------------------------

def test_parse_args_empty_is_defaults() -> None:
    assert mrs._parse_args("") == ({}, set())


def test_parse_args_filters_and_flags() -> None:
    filters, flags = mrs._parse_args("author=@me,state=merged,nopipe")
    assert filters == {"author": "@me", "state": "merged"}
    assert flags == {"nopipe"}


def test_parse_args_iids_flag() -> None:
    filters, flags = mrs._parse_args("reviewer=@me,iids")
    assert filters == {"reviewer": "@me"}
    assert flags == {"iids"}


def test_parse_args_failed_flag() -> None:
    filters, flags = mrs._parse_args("author=@me,failed,iids")
    assert filters == {"author": "@me"}
    assert flags == {"failed", "iids"}


def test_parse_args_ignores_unknown_bare_token() -> None:
    """A bare token that isn't a known flag is dropped, not treated as filter."""
    filters, flags = mrs._parse_args("bogus,author=x")
    assert filters == {"author": "x"}
    assert flags == set()


# ---------------------------------------------------------------------------
# _build_list_cmd
# ---------------------------------------------------------------------------

def test_build_cmd_defaults_to_author_me() -> None:
    cmd = mrs._build_list_cmd({}, 50)
    assert "--author" in cmd and "@me" in cmd
    assert cmd[:5] == ["glab", "mr", "list", "-F", "json"]


def test_build_cmd_role_filter_suppresses_default_author() -> None:
    """Explicit reviewer means we must NOT also inject --author @me."""
    cmd = mrs._build_list_cmd({"reviewer": "@me"}, 50)
    assert "--reviewer" in cmd
    assert "--author" not in cmd


def test_build_cmd_state_maps_to_flag() -> None:
    assert "--merged" in mrs._build_list_cmd({"author": "x", "state": "merged"}, 50)
    assert "--closed" in mrs._build_list_cmd({"author": "x", "state": "closed"}, 50)


def test_build_cmd_state_opened_emits_no_flag() -> None:
    """opened is glab's default — no flag, and not a stray --opened."""
    cmd = mrs._build_list_cmd({"author": "x", "state": "opened"}, 50)
    assert "--opened" not in cmd
    assert "--merged" not in cmd


def test_build_cmd_milestone_and_label() -> None:
    cmd = mrs._build_list_cmd({"author": "x", "milestone": "v18.9", "label": "Bug"}, 10)
    assert cmd[cmd.index("--milestone") + 1] == "v18.9"
    assert cmd[cmd.index("--label") + 1] == "Bug"
    assert cmd[cmd.index("-P") + 1] == "10"


# ---------------------------------------------------------------------------
# _watched_iids
# ---------------------------------------------------------------------------

def test_watched_iids_reads_live_pid(tmp_path, monkeypatch) -> None:
    """A PID file whose process is alive marks that iid as watched."""
    pid_file = tmp_path / "supertool-watch-gitlab-mr__22504.pid"
    pid_file.write_text("12345\n")
    monkeypatch.setattr(mrs, "_pid_alive", lambda _p: True)
    assert mrs._watched_iids(str(tmp_path)) == {"22504"}


def test_watched_iids_skips_stale_pid(tmp_path, monkeypatch) -> None:
    """A PID file whose process is dead does NOT count as watched."""
    (tmp_path / "supertool-watch-gitlab-mr__999.pid").write_text("404\n")
    monkeypatch.setattr(mrs, "_pid_alive", lambda _p: False)
    assert mrs._watched_iids(str(tmp_path)) == set()


def test_watched_iids_ignores_other_sources(tmp_path, monkeypatch) -> None:
    """github-pr watchers must not leak into the gitlab-mr watched set."""
    (tmp_path / "supertool-watch-github-pr__7.pid").write_text("1\n")
    monkeypatch.setattr(mrs, "_pid_alive", lambda _p: True)
    assert mrs._watched_iids(str(tmp_path)) == set()


def test_watched_iids_handles_garbage_pid(tmp_path, monkeypatch) -> None:
    (tmp_path / "supertool-watch-gitlab-mr__5.pid").write_text("not-a-number")
    monkeypatch.setattr(mrs, "_pid_alive", lambda _p: True)
    assert mrs._watched_iids(str(tmp_path)) == set()


# ---------------------------------------------------------------------------
# _get_config
# ---------------------------------------------------------------------------

def test_get_config_defaults(monkeypatch) -> None:
    for k in ("SUPERTOOL_ENRICH_WORKERS", "SUPERTOOL_ENRICH_CAP", "SUPERTOOL_PER_PAGE"):
        monkeypatch.delenv(k, raising=False)
    cfg = mrs._get_config()
    assert cfg == {
        "enrich_workers": mrs.ENRICH_WORKERS,
        "enrich_cap": mrs.ENRICH_CAP,
        "per_page": mrs.DEFAULT_PER_PAGE,
    }


def test_get_config_env_override(monkeypatch) -> None:
    monkeypatch.setenv("SUPERTOOL_ENRICH_WORKERS", "3")
    monkeypatch.setenv("SUPERTOOL_ENRICH_CAP", "10")
    monkeypatch.setenv("SUPERTOOL_PER_PAGE", "25")
    cfg = mrs._get_config()
    assert cfg == {"enrich_workers": 3, "enrich_cap": 10, "per_page": 25}


def test_get_config_bad_value_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("SUPERTOOL_ENRICH_WORKERS", "not-a-number")
    assert mrs._get_config()["enrich_workers"] == mrs.ENRICH_WORKERS


def test_get_config_clamps_workers_floor(monkeypatch) -> None:
    """workers must never drop below 1 (ThreadPoolExecutor rejects 0)."""
    monkeypatch.setenv("SUPERTOOL_ENRICH_WORKERS", "0")
    assert mrs._get_config()["enrich_workers"] == 1


# ---------------------------------------------------------------------------
# _enrich_pipelines
# ---------------------------------------------------------------------------

def _detail(status="success", url="u", pid="9", changes=5) -> dict:
    return {"head_pipeline": {"status": status, "web_url": url, "id": pid}, "changes_count": changes}


def test_enrich_fills_all_fields(monkeypatch) -> None:
    monkeypatch.setattr(mrs, "_fetch_mr_detail", lambda iid: _detail(changes=int(iid) * 2))
    monkeypatch.setattr(mrs, "_fetch_approvals", lambda iid: {"approved": True, "approved_by": ["fdavid"]})
    monkeypatch.setattr(mrs, "_fetch_failed_jobs", lambda pid: [])
    mr_list = [{"iid": 1}, {"iid": 2}]
    mrs._enrich(mr_list)
    assert mr_list[0]["_pipeline"] == "success"
    assert mr_list[0]["_changes"] == 2
    assert mr_list[1]["_changes"] == 4
    assert mr_list[0]["_approved"] is True
    assert mr_list[0]["_approved_by"] == ["fdavid"]


def test_enrich_coerces_string_changes_count(monkeypatch) -> None:
    """GitLab returns changes_count as a string — it must become an int."""
    monkeypatch.setattr(mrs, "_fetch_mr_detail", lambda iid: {"head_pipeline": {"status": "success"}, "changes_count": "20"})
    monkeypatch.setattr(mrs, "_fetch_approvals", lambda iid: {})
    monkeypatch.setattr(mrs, "_fetch_failed_jobs", lambda pid: [])
    mr_list = [{"iid": 1}]
    mrs._enrich(mr_list)
    assert mr_list[0]["_changes"] == 20


def test_enrich_fetches_failed_jobs_only_for_failing(monkeypatch) -> None:
    """Wave 2 (failed-job names) fires only for MRs whose pipeline failed."""
    monkeypatch.setattr(
        mrs, "_fetch_mr_detail",
        lambda iid: _detail(status="failed" if iid == "1" else "success", pid=iid),
    )
    monkeypatch.setattr(mrs, "_fetch_approvals", lambda iid: {})
    seen = []
    monkeypatch.setattr(mrs, "_fetch_failed_jobs", lambda pid: seen.append(pid) or ["phpstan2"])
    mr_list = [{"iid": 1}, {"iid": 2}]
    mrs._enrich(mr_list)
    assert seen == ["1"]  # only the failing MR's pipeline was queried
    assert mr_list[0]["_failed_jobs"] == ["phpstan2"]
    assert mr_list[1]["_failed_jobs"] == []


def test_enrich_skips_approvals_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(mrs, "_fetch_mr_detail", lambda iid: _detail())
    monkeypatch.setattr(mrs, "_fetch_failed_jobs", lambda pid: [])
    called = []
    monkeypatch.setattr(mrs, "_fetch_approvals", lambda iid: called.append(iid) or {})
    mrs._enrich([{"iid": 1}], with_approvals=False)
    assert called == []


def test_enrich_caps_calls(monkeypatch) -> None:
    """Never fetch detail for more than ENRICH_CAP MRs."""
    calls = []
    monkeypatch.setattr(mrs, "_fetch_mr_detail", lambda iid: calls.append(iid) or _detail())
    monkeypatch.setattr(mrs, "_fetch_approvals", lambda iid: {})
    monkeypatch.setattr(mrs, "_fetch_failed_jobs", lambda pid: [])
    mrs._enrich([{"iid": i} for i in range(mrs.ENRICH_CAP + 10)])
    assert len(calls) == mrs.ENRICH_CAP


# ---------------------------------------------------------------------------
# _fetch_approvals / _fetch_failed_jobs parsing
# ---------------------------------------------------------------------------

def test_fetch_approvals_parses_usernames(monkeypatch) -> None:
    monkeypatch.setattr(
        mrs, "_api_json",
        lambda ep, timeout=10: {"approved": True, "approved_by": [{"user": {"username": "fdavid"}}]},
    )
    assert mrs._fetch_approvals("1") == {"approved": True, "approved_by": ["fdavid"]}


def test_fetch_approvals_empty_on_failure(monkeypatch) -> None:
    monkeypatch.setattr(mrs, "_api_json", lambda ep, timeout=10: None)
    assert mrs._fetch_approvals("1") == {}


def test_fetch_failed_jobs_returns_names(monkeypatch) -> None:
    monkeypatch.setattr(
        mrs, "_api_json",
        lambda ep, timeout=10: [{"name": "phpstan2"}, {"name": "test_unit_dpt"}],
    )
    assert mrs._fetch_failed_jobs("9") == ["phpstan2", "test_unit_dpt"]


def test_fetch_failed_jobs_empty_on_failure(monkeypatch) -> None:
    monkeypatch.setattr(mrs, "_api_json", lambda ep, timeout=10: None)
    assert mrs._fetch_failed_jobs("9") == []


# ---------------------------------------------------------------------------
# _pipe_glyph
# ---------------------------------------------------------------------------

def test_pipe_glyph_known_states() -> None:
    assert "failed" in mrs._pipe_glyph("failed", True)
    assert "running" in mrs._pipe_glyph("running", True)
    assert "ok" in mrs._pipe_glyph("success", True)


def test_pipe_glyph_empty_is_none() -> None:
    assert mrs._pipe_glyph("", True) == "? none"


# ---------------------------------------------------------------------------
# _pipe_cell / _appr_cell / _sort_key
# ---------------------------------------------------------------------------

def test_pipe_cell_shows_failed_job_name() -> None:
    """A failed pipeline shows the job name — the failure class in one word."""
    assert mrs._pipe_cell({"_pipeline": "failed", "_failed_jobs": ["phpstan2"]}, True) == "✗ phpstan2"


def test_pipe_cell_multiple_failed_jobs_counts_extra() -> None:
    out = mrs._pipe_cell({"_pipeline": "failed", "_failed_jobs": ["phpstan2", "rector", "md"]}, True)
    assert out == "✗ phpstan2 +2"


def test_pipe_cell_failed_without_job_names() -> None:
    assert mrs._pipe_cell({"_pipeline": "failed", "_failed_jobs": []}, True) == "✗ failed"


def test_appr_cell_states() -> None:
    assert mrs._appr_cell({"_approved": True}) == "✓"
    assert mrs._appr_cell({"_approved": False}) == "·"
    assert mrs._appr_cell({}) == " "


def test_sort_key_failing_first_then_stalest() -> None:
    mr_list = [
        {"iid": 1, "_pipeline": "success", "updated_at": "2026-01-01"},
        {"iid": 2, "_pipeline": "failed", "updated_at": "2026-05-01"},
        {"iid": 3, "_pipeline": "failed", "updated_at": "2026-01-01"},
    ]
    order = [m["iid"] for m in sorted(mr_list, key=mrs._sort_key)]
    assert order == [3, 2, 1]  # failing first (oldest failing first), success last


def test_pipe_glyph_nopipe_is_dash() -> None:
    assert mrs._pipe_glyph("failed", False) == "—"


def test_pipe_glyph_unknown_status_passthrough() -> None:
    assert mrs._pipe_glyph("weird", True) == "weird"


# ---------------------------------------------------------------------------
# _render_table
# ---------------------------------------------------------------------------

def test_render_table_marks_watched() -> None:
    mr_list = [{"iid": 100, "title": "x", "_pipeline": "failed"}]
    out = mrs._render_table(mr_list, {"100"}, True)
    assert "👁" in out
    assert "!100" in out


def test_render_table_flags_conflict_and_draft() -> None:
    mr_list = [{"iid": 1, "title": "t", "draft": True, "has_conflicts": True, "_pipeline": "success"}]
    out = mrs._render_table(mr_list, set(), True)
    assert "draft" in out and "conflict" in out


def test_render_table_conflict_via_detailed_merge_status() -> None:
    mr_list = [{"iid": 2, "title": "t", "detailed_merge_status": "conflict", "_pipeline": ""}]
    out = mrs._render_table(mr_list, set(), True)
    assert "conflict" in out


def test_render_table_empty() -> None:
    assert mrs._render_table([], set(), True) == "No MRs match."


# ---------------------------------------------------------------------------
# _footer
# ---------------------------------------------------------------------------

def test_footer_points_at_first_unwatched_failure() -> None:
    mr_list = [
        {"iid": 10, "_pipeline": "failed"},
        {"iid": 11, "_pipeline": "failed"},
        {"iid": 12, "_pipeline": "success"},
    ]
    out = mrs._footer(mr_list, {"10"}, True)
    assert "2 failing" in out
    assert "1 unwatched" in out
    assert "watch:gitlab-mr:11" in out  # 10 is watched, 11 is the first unwatched


def test_footer_counts_unapproved() -> None:
    mr_list = [
        {"iid": 1, "_pipeline": "success", "_approved": False},
        {"iid": 2, "_pipeline": "success", "_approved": True},
    ]
    out = mrs._footer(mr_list, set(), True)
    assert "1 unapproved" in out


def test_footer_no_failures_just_count() -> None:
    out = mrs._footer([{"iid": 1, "_pipeline": "success"}], set(), True)
    assert "1 MR(s)" in out
    assert "failing" not in out


def test_footer_empty_when_nopipe() -> None:
    assert mrs._footer([{"iid": 1, "_pipeline": "failed"}], set(), False) == ""


# ---------------------------------------------------------------------------
# _age
# ---------------------------------------------------------------------------

def test_age_future_timestamp_is_now() -> None:
    """Clock skew (updated_at in the future) must not render '-1d'."""
    assert mrs._age("2999-01-01T00:00:00Z") == "now"


def test_age_bad_iso_is_empty() -> None:
    assert mrs._age("not-a-date") == ""


def test_age_empty_is_empty() -> None:
    assert mrs._age("") == ""


# ---------------------------------------------------------------------------
# _flags
# ---------------------------------------------------------------------------

def test_flags_threads_only_when_unresolved() -> None:
    assert "threads" in mrs._flags({"blocking_discussions_resolved": False})
    assert mrs._flags({"blocking_discussions_resolved": True}) == ""
    assert mrs._flags({}) == ""  # field absent → no flag


# ---------------------------------------------------------------------------
# main() integration — the watch-mine.sh contract
# ---------------------------------------------------------------------------

def _fake_list(stdout: str, returncode: int = 0):
    def _run(cmd, timeout=25):
        return subprocess.CompletedProcess(cmd, returncode, stdout, "")
    return _run


def test_main_iids_outputs_bare_ids(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["mrs.py", "author=@me,nopipe,iids"])
    monkeypatch.setattr(mrs, "_run", _fake_list('[{"iid": 10}, {"iid": 20}]'))
    rc = mrs.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert out.split() == ["10", "20"]  # bare ids, nothing else


def test_main_iids_skips_missing_iid(monkeypatch, capsys) -> None:
    """A row without an iid must not print 'None' into the watch feed."""
    monkeypatch.setattr(sys, "argv", ["mrs.py", "nopipe,iids"])
    monkeypatch.setattr(mrs, "_run", _fake_list('[{"iid": 10}, {"title": "no iid"}]'))
    mrs.main()
    out = capsys.readouterr().out
    assert "None" not in out
    assert out.split() == ["10"]


def test_main_failed_filters_to_failing(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["mrs.py", "author=@me,failed,iids"])
    monkeypatch.setattr(mrs, "_run", _fake_list('[{"iid": 10}, {"iid": 20}]'))

    def fake_enrich(mr_list, *a, **k):
        for m in mr_list:
            m["_pipeline"] = "failed" if m["iid"] == 10 else "success"
    monkeypatch.setattr(mrs, "_enrich", fake_enrich)
    rc = mrs.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert out.split() == ["10"]  # only the failing MR survives the filter


def test_main_glab_error_returns_1(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["mrs.py", ""])
    monkeypatch.setattr(mrs, "_run", _fake_list("", returncode=1))
    rc = mrs.main()
    assert rc == 1


def test_main_bad_json_returns_1(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["mrs.py", "nopipe"])
    monkeypatch.setattr(mrs, "_run", _fake_list("not json"))
    assert mrs.main() == 1
