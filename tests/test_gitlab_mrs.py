"""Tests for the gl-mrs triage-board op.

Covers the pure helpers — arg parsing, glab-cmd construction, watch-state
cross-reference, pipeline glyph mapping, table render, and footer hint —
without hitting the live glab CLI.
"""
from __future__ import annotations

import importlib.util
import json
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


def test_get_config_refuses_workers_below_floor(monkeypatch, capsys) -> None:
    """workers must never drop below 1 (ThreadPoolExecutor rejects 0).

    The invariant is unchanged; where an out-of-range value lands is not.
    This used to `max(1, ...)` its way to exactly 1, in silence — so
    `SUPERTOOL_ENRICH_WORKERS=0` read identically to `=1`, and a caller who had
    asked for something impossible was never told. #654 makes the floor a
    *validated minimum*: the value is refused, the default is used, and the
    swap is stated. Deliberate behaviour change, not an accident of the
    refactor — see the helper's docstring for why an announced fallback beats a
    silent clamp.
    """
    monkeypatch.setenv("SUPERTOOL_ENRICH_WORKERS", "0")
    workers = mrs._get_config()["enrich_workers"]
    assert workers == mrs.ENRICH_WORKERS
    assert workers >= 1
    out = capsys.readouterr().out
    assert "SUPERTOOL_ENRICH_WORKERS" in out
    assert f"using {mrs.ENRICH_WORKERS}" in out


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
# _branches / two-line rows — the board is read by a human (#421)
# ---------------------------------------------------------------------------

def test_branches_renders_source_arrow_target() -> None:
    m = {"source_branch": "max/generator-testability-coverage", "target_branch": "master"}
    assert mrs._branches(m) == "max/generator-testability-coverage -> master"


def test_branches_marks_a_missing_side_rather_than_dropping_the_pair() -> None:
    assert mrs._branches({"source_branch": "max/foo"}) == "max/foo -> ?"
    assert mrs._branches({"target_branch": "master"}) == "? -> master"


def test_branches_is_empty_when_neither_side_is_known() -> None:
    """'? -> ?' is noise, not information."""
    assert mrs._branches({}) == ""
    assert mrs._branches({"source_branch": "", "target_branch": None}) == ""


def test_row_renders_a_long_title_in_full_on_its_own_line() -> None:
    title = "Make the Generator module loadable and cover TemplatesDescriptor"
    assert len(title) > 42, "fixture must exceed the old 42-char truncation"
    row = mrs._row({"iid": 33173, "title": title, "source_branch": "max/gen",
                    "target_branch": "master", "_pipeline": "failed"}, set(), True)
    status, title_line = row.split("\n")
    assert "max/gen -> master" in status
    assert title not in status
    assert title_line == f"{mrs.TITLE_INDENT}{title}"


def test_row_keeps_flags_on_the_status_line() -> None:
    row = mrs._row({"iid": 1, "title": "t", "source_branch": "b",
                    "target_branch": "master", "draft": True,
                    "_pipeline": "success"}, set(), True)
    assert row.split("\n")[0].endswith("b -> master [draft]")


def test_row_without_a_title_is_a_single_line() -> None:
    """No blank second line for an MR whose title we never got."""
    row = mrs._row({"iid": 1, "source_branch": "b", "target_branch": "master",
                    "_pipeline": "success"}, set(), True)
    assert "\n" not in row
    assert row.endswith("b -> master")


def test_row_suffix_is_appended_to_the_status_line_not_the_title() -> None:
    row = mrs._row({"iid": 1, "title": "prose", "source_branch": "b",
                    "target_branch": "master", "_pipeline": "success"},
                   set(), True, "  [healed]")
    status, title_line = row.split("\n")
    assert status.endswith("[healed]")
    assert title_line == f"{mrs.TITLE_INDENT}prose"


def test_render_table_emits_two_lines_per_mr() -> None:
    mr_list = [
        {"iid": 1, "title": "first", "source_branch": "a", "target_branch": "master",
         "_pipeline": "success"},
        {"iid": 2, "title": "second", "source_branch": "b", "target_branch": "master",
         "_pipeline": "success"},
    ]
    assert len(mrs._render_table(mr_list, set(), True).splitlines()) == 4


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


# ---------------------------------------------------------------------------
# _parse_multi / _expand_filters — the plural reading of one filter vocabulary
# ---------------------------------------------------------------------------

def test_parse_multi_keeps_every_value_of_a_repeated_key() -> None:
    filters, flags = mrs._parse_multi("author=@me,author=modular.system,state=opened")
    assert filters == {"author": ["@me", "modular.system"], "state": ["opened"]}
    assert flags == set()


def test_parse_multi_agrees_with_parse_args_on_flags() -> None:
    assert mrs._parse_multi("reviewer=@me,iids")[1] == {"iids"}


def test_parse_args_resolves_a_repeated_key_to_one_value() -> None:
    """gl-mrs issues one query, so its view of a repeated key stays scalar."""
    filters, _flags = mrs._parse_args("author=@me,author=modular.system")
    assert filters == {"author": "modular.system"}


def test_expand_filters_fans_a_repeated_key_into_one_dict_per_value() -> None:
    multi, _flags = mrs._parse_multi("author=@me,author=modular.system,state=opened")
    assert mrs._expand_filters(multi) == [
        {"author": "@me", "state": "opened"},
        {"author": "modular.system", "state": "opened"},
    ]


def test_expand_filters_leaves_a_scalar_filter_exactly_as_parse_args_saw_it() -> None:
    """The single-query path must produce byte-identical argv, or every caller
    of the plural parser quietly changes the command it was already sending."""
    arg = "author=@me,state=opened"
    scalar, _f = mrs._parse_args(arg)
    multi, _g = mrs._parse_multi(arg)
    assert mrs._expand_filters(multi) == [scalar]
    assert (mrs._build_list_cmd(mrs._expand_filters(multi)[0], 50)
            == mrs._build_list_cmd(scalar, 50))


def test_expand_filters_of_an_empty_filter_is_one_unfiltered_query() -> None:
    assert mrs._expand_filters({}) == [{}]

# ---------------------------------------------------------------------------
# [conflict] vs [empty] — has_conflicts is not a conflict field (#471)
#
# Driven end to end over the payloads the API actually returns: the `glab mr
# list` JSON on stdout, and the per-MR detail `_enrich` already fetches. A test
# against `_flags` alone would pass on a half-implementation that never wires
# the detail signal through enrichment.
# ---------------------------------------------------------------------------

def _list_row(iid=1, **kw) -> dict:
    """One row as `glab mr list -F json` returns it: `detailed_merge_status`
    and `sha` on every row, never `diff_refs`."""
    row = {
        "iid": iid,
        "title": "t",
        "draft": False,
        "source_branch": f"b{iid}",
        "target_branch": "master",
        "updated_at": "2026-07-27T10:00:00Z",
        "has_conflicts": True,
        "detailed_merge_status": "cannot_be_merged",
        "sha": "a" * 40,
        "blocking_discussions_resolved": True,
    }
    row.update(kw)
    return row


def _detail_payload(**kw) -> dict:
    d = {"head_pipeline": {"status": "success", "id": "9"}, "changes_count": "3"}
    d.update(kw)
    return d


def _drive(monkeypatch, capsys, rows, details=None) -> str:
    """Run `gl-mrs` over stubbed API payloads and return the rendered board."""
    details = details or {}
    monkeypatch.setattr(sys, "argv", ["mrs.py", ""])
    monkeypatch.setattr(
        mrs, "_run",
        lambda cmd, timeout=25: subprocess.CompletedProcess(cmd, 0, json.dumps(rows), ""),
    )

    def _api(endpoint, timeout=10):
        if endpoint.endswith("/approvals"):
            return {}
        iid = endpoint.rsplit("/", 1)[-1]
        return details.get(iid, _detail_payload())

    monkeypatch.setattr(mrs, "_api_json", _api)
    monkeypatch.setattr(mrs, "_watched_iids", lambda *a, **k: set())
    assert mrs.main() == 0
    return capsys.readouterr().out


def test_an_mr_with_no_commits_is_not_rendered_as_conflicted(monkeypatch, capsys) -> None:
    """`commits_status` is GitLab's identifier for a source branch with no
    commits. There is no diff, so there is nothing that can conflict."""
    out = _drive(monkeypatch, capsys, [_list_row(detailed_merge_status="commits_status")])
    assert "conflict" not in out
    assert "empty" in out


def test_an_mr_with_a_null_sha_is_not_rendered_as_conflicted(monkeypatch, capsys) -> None:
    """!33223 in #465: opened before any push, `sha: null`, `has_conflicts: true`."""
    out = _drive(monkeypatch, capsys, [_list_row(sha=None)])
    assert "conflict" not in out
    assert "empty" in out


def test_an_empty_diff_is_detected_from_the_enriched_detail_payload(monkeypatch, capsys) -> None:
    """The signal the list endpoint omits. `diff_refs` never ships in the list
    row, but `_enrich` already fetches the detail endpoint for every MR on the
    board, so the poller's strongest signal costs no extra call here."""
    rows = [_list_row(iid=7)]
    details = {"7": _detail_payload(diff_refs={"base_sha": "b" * 40, "head_sha": "b" * 40})}
    out = _drive(monkeypatch, capsys, rows, details)
    assert "conflict" not in out
    assert "empty" in out


def test_a_genuine_conflict_is_still_flagged(monkeypatch, capsys) -> None:
    """A settled MR with a real diff that really conflicts must still say so."""
    rows = [_list_row(iid=8)]
    details = {"8": _detail_payload(diff_refs={"base_sha": "b" * 40, "head_sha": "c" * 40})}
    out = _drive(monkeypatch, capsys, rows, details)
    assert "conflict" in out
    assert "empty" not in out


def test_a_conflict_is_still_flagged_when_the_detail_payload_is_absent(monkeypatch, capsys) -> None:
    """Absent fields are never evidence. A detail fetch that failed leaves
    `has_conflicts` trusted, so the guard cannot argue itself into silence
    about a conflict it merely failed to observe."""
    out = _drive(monkeypatch, capsys, [_list_row(iid=9)], {"9": {}})
    assert "conflict" in out
    assert "empty" not in out
