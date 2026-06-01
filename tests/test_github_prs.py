"""Tests for the gh-prs triage-board op.

Covers the pure helpers — arg parsing, gh-cmd construction, check-rollup
reduction, watch-state cross-reference, render, sort, footer — plus the
main() watch-mine.sh contract, without hitting the live gh CLI.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

PRESET_PATH = Path(__file__).parent.parent / "presets" / "github" / "prs.py"
_spec = importlib.util.spec_from_file_location("github_prs", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
prs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prs)


# ---------------------------------------------------------------------------
# _parse_args
# ---------------------------------------------------------------------------

def test_parse_args_empty_is_defaults() -> None:
    assert prs._parse_args("") == ({}, set())


def test_parse_args_filters_and_flags() -> None:
    filters, flags = prs._parse_args("author=@me,state=merged,nopipe")
    assert filters == {"author": "@me", "state": "merged"}
    assert flags == {"nopipe"}


def test_parse_args_iids_flag() -> None:
    _, flags = prs._parse_args("author=@me,iids")
    assert "iids" in flags


def test_parse_args_failed_flag() -> None:
    _, flags = prs._parse_args("failed")
    assert flags == {"failed"}


def test_parse_args_ignores_unknown_bare_token() -> None:
    filters, flags = prs._parse_args("author=@me,bogus")
    assert flags == set()
    assert filters == {"author": "@me"}


# ---------------------------------------------------------------------------
# _build_list_cmd
# ---------------------------------------------------------------------------

def test_build_cmd_defaults_to_author_me() -> None:
    cmd = prs._build_list_cmd({}, 50)
    assert "--author" in cmd and "@me" in cmd
    assert cmd[:4] == ["gh", "pr", "list", "--json"]
    assert cmd[cmd.index("--limit") + 1] == "50"


def test_build_cmd_role_filter_suppresses_default_author() -> None:
    cmd = prs._build_list_cmd({"reviewer": "@me"}, 50)
    assert "--search" in cmd
    assert cmd[cmd.index("--search") + 1] == "review-requested:@me"
    assert "--author" not in cmd


def test_build_cmd_assignee_suppresses_default_author() -> None:
    cmd = prs._build_list_cmd({"assignee": "fdavid"}, 50)
    assert cmd[cmd.index("--assignee") + 1] == "fdavid"
    assert "--author" not in cmd


def test_build_cmd_state_maps_to_flag() -> None:
    assert "merged" in prs._build_list_cmd({"author": "x", "state": "merged"}, 50)
    assert "closed" in prs._build_list_cmd({"author": "x", "state": "closed"}, 50)


def test_build_cmd_state_open_emits_no_flag() -> None:
    """open is gh's default — no --state flag emitted."""
    cmd = prs._build_list_cmd({"author": "x", "state": "open"}, 50)
    assert "--state" not in cmd


def test_build_cmd_label_and_limit() -> None:
    cmd = prs._build_list_cmd({"author": "x", "label": "bug"}, 10)
    assert cmd[cmd.index("--label") + 1] == "bug"
    assert cmd[cmd.index("--limit") + 1] == "10"


# ---------------------------------------------------------------------------
# check rollup reduction
# ---------------------------------------------------------------------------

def test_check_failed_handles_checkrun_and_statuscontext() -> None:
    assert prs._check_failed({"conclusion": "FAILURE"})
    assert prs._check_failed({"state": "ERROR"})
    assert not prs._check_failed({"conclusion": "SUCCESS"})
    assert not prs._check_failed({"state": "SUCCESS"})


def test_rollup_state_priorities() -> None:
    assert prs._rollup_state([]) == ""
    assert prs._rollup_state([{"conclusion": "SUCCESS"}]) == "success"
    assert prs._rollup_state([{"status": "IN_PROGRESS"}]) == "running"
    assert prs._rollup_state([{"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}]) == "failed"


def test_failed_check_names_uses_name_then_context() -> None:
    checks = [
        {"conclusion": "FAILURE", "name": "phpstan"},
        {"state": "FAILURE", "context": "ci/circleci"},
        {"conclusion": "SUCCESS", "name": "ok"},
    ]
    assert prs._failed_check_names(checks) == ["phpstan", "ci/circleci"]


# ---------------------------------------------------------------------------
# _annotate
# ---------------------------------------------------------------------------

def test_annotate_derives_all_fields() -> None:
    pr_list = [{
        "statusCheckRollup": [{"conclusion": "FAILURE", "name": "phpstan"}],
        "additions": 12, "deletions": 8,
        "reviewDecision": "APPROVED",
    }]
    prs._annotate(pr_list)
    p = pr_list[0]
    assert p["_checks"] == "failed"
    assert p["_failed_checks"] == ["phpstan"]
    assert p["_changes"] == 20
    assert p["_approved"] is True


def test_annotate_approval_states() -> None:
    pr_list = [
        {"reviewDecision": "CHANGES_REQUESTED"},
        {"reviewDecision": "REVIEW_REQUIRED"},
        {"reviewDecision": ""},
        {"reviewDecision": None},
    ]
    prs._annotate(pr_list)
    assert pr_list[0]["_approved"] is False
    assert pr_list[1]["_approved"] is False
    assert pr_list[2]["_approved"] is None
    assert pr_list[3]["_approved"] is None


def test_annotate_changes_none_when_missing() -> None:
    pr_list = [{"reviewDecision": "APPROVED"}]
    prs._annotate(pr_list)
    assert pr_list[0]["_changes"] is None


def test_annotate_handles_non_list_rollup() -> None:
    pr_list = [{"statusCheckRollup": None, "reviewDecision": ""}]
    prs._annotate(pr_list)
    assert pr_list[0]["_checks"] == ""


# ---------------------------------------------------------------------------
# _enrich
# ---------------------------------------------------------------------------

def test_enrich_counts_unresolved_threads(monkeypatch) -> None:
    monkeypatch.setattr(
        prs, "_fetch_review_threads",
        lambda url, n: [{"isResolved": False}, {"isResolved": True}, {"isResolved": False}],
    )
    pr_list = [{"number": 1, "url": "u"}]
    prs._enrich(pr_list)
    assert pr_list[0]["_unresolved"] == 2


def test_enrich_caps_calls(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        prs, "_fetch_review_threads",
        lambda url, n: calls.append(n) or [],
    )
    prs._enrich([{"number": i, "url": "u"} for i in range(prs.ENRICH_CAP + 5)])
    assert len(calls) == prs.ENRICH_CAP


# ---------------------------------------------------------------------------
# _get_config
# ---------------------------------------------------------------------------

def test_get_config_defaults(monkeypatch) -> None:
    for k in ("SUPERTOOL_ENRICH_WORKERS", "SUPERTOOL_ENRICH_CAP", "SUPERTOOL_PER_PAGE"):
        monkeypatch.delenv(k, raising=False)
    assert prs._get_config() == {
        "enrich_workers": prs.ENRICH_WORKERS,
        "enrich_cap": prs.ENRICH_CAP,
        "per_page": prs.DEFAULT_PER_PAGE,
    }


def test_get_config_env_override(monkeypatch) -> None:
    monkeypatch.setenv("SUPERTOOL_ENRICH_WORKERS", "3")
    monkeypatch.setenv("SUPERTOOL_ENRICH_CAP", "10")
    monkeypatch.setenv("SUPERTOOL_PER_PAGE", "25")
    assert prs._get_config() == {"enrich_workers": 3, "enrich_cap": 10, "per_page": 25}


def test_get_config_bad_value_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("SUPERTOOL_ENRICH_WORKERS", "nope")
    assert prs._get_config()["enrich_workers"] == prs.ENRICH_WORKERS


def test_get_config_clamps_workers_floor(monkeypatch) -> None:
    monkeypatch.setenv("SUPERTOOL_ENRICH_WORKERS", "0")
    assert prs._get_config()["enrich_workers"] == 1


# ---------------------------------------------------------------------------
# _watched_numbers
# ---------------------------------------------------------------------------

def test_watched_numbers_reads_live_pid(tmp_path, monkeypatch) -> None:
    (tmp_path / "supertool-watch-github-pr__274.pid").write_text("12345\n")
    monkeypatch.setattr(prs, "_pid_alive", lambda _p: True)
    assert prs._watched_numbers(str(tmp_path)) == {"274"}


def test_watched_numbers_skips_stale_pid(tmp_path, monkeypatch) -> None:
    (tmp_path / "supertool-watch-github-pr__999.pid").write_text("404\n")
    monkeypatch.setattr(prs, "_pid_alive", lambda _p: False)
    assert prs._watched_numbers(str(tmp_path)) == set()


def test_watched_numbers_ignores_other_sources(tmp_path, monkeypatch) -> None:
    (tmp_path / "supertool-watch-gitlab-mr__7.pid").write_text("1\n")
    monkeypatch.setattr(prs, "_pid_alive", lambda _p: True)
    assert prs._watched_numbers(str(tmp_path)) == set()


def test_watched_numbers_handles_garbage_pid(tmp_path, monkeypatch) -> None:
    (tmp_path / "supertool-watch-github-pr__5.pid").write_text("not-a-number")
    monkeypatch.setattr(prs, "_pid_alive", lambda _p: True)
    assert prs._watched_numbers(str(tmp_path)) == set()


# ---------------------------------------------------------------------------
# cells / sort / age / flags
# ---------------------------------------------------------------------------

def test_check_cell_shows_failed_check_name() -> None:
    assert prs._check_cell({"_checks": "failed", "_failed_checks": ["phpstan"]}) == "✗ phpstan"


def test_check_cell_multiple_failed_counts_extra() -> None:
    out = prs._check_cell({"_checks": "failed", "_failed_checks": ["phpstan", "rector", "md"]})
    assert out == "✗ phpstan +2"


def test_check_cell_failed_without_names() -> None:
    assert prs._check_cell({"_checks": "failed", "_failed_checks": []}) == "✗ failed"


def test_check_cell_no_checks_is_none() -> None:
    assert prs._check_cell({"_checks": ""}) == "? none"


def test_appr_cell_states() -> None:
    assert prs._appr_cell({"_approved": True}) == "✓"
    assert prs._appr_cell({"_approved": False}) == "·"
    assert prs._appr_cell({}) == " "


def test_sort_key_failing_first_then_stalest() -> None:
    pr_list = [
        {"number": 1, "_checks": "success", "updatedAt": "2026-01-01"},
        {"number": 2, "_checks": "failed", "updatedAt": "2026-05-01"},
        {"number": 3, "_checks": "failed", "updatedAt": "2026-01-01"},
    ]
    order = [p["number"] for p in sorted(pr_list, key=prs._sort_key)]
    assert order == [3, 2, 1]


def test_age_future_is_now() -> None:
    assert prs._age("2999-01-01T00:00:00Z") == "now"


def test_age_bad_iso_is_empty() -> None:
    assert prs._age("not-a-date") == ""


def test_flags_draft_conflict_threads() -> None:
    out = prs._flags({"isDraft": True, "mergeable": "CONFLICTING", "_unresolved": 2})
    assert "draft" in out and "conflict" in out and "threads" in out


def test_flags_empty_when_clean() -> None:
    assert prs._flags({"isDraft": False, "mergeable": "MERGEABLE", "_unresolved": 0}) == ""


# ---------------------------------------------------------------------------
# _render_table / _footer
# ---------------------------------------------------------------------------

def test_render_table_marks_watched() -> None:
    pr_list = [{"number": 100, "title": "x", "_checks": "failed"}]
    out = prs._render_table(pr_list, {"100"})
    assert "👁" in out
    assert "#100" in out


def test_render_table_empty() -> None:
    assert prs._render_table([], set()) == "No PRs match."


def test_footer_points_at_first_unwatched_failure() -> None:
    pr_list = [
        {"number": 10, "_checks": "failed"},
        {"number": 11, "_checks": "failed"},
        {"number": 12, "_checks": "success"},
    ]
    out = prs._footer(pr_list, {"10"})
    assert "2 failing" in out
    assert "1 unwatched" in out
    assert "watch:github-pr:11" in out


def test_footer_counts_unapproved() -> None:
    pr_list = [
        {"number": 1, "_checks": "success", "_approved": False},
        {"number": 2, "_checks": "success", "_approved": True},
    ]
    assert "1 unapproved" in prs._footer(pr_list, set())


# ---------------------------------------------------------------------------
# main() integration — the watch-mine.sh contract
# ---------------------------------------------------------------------------

def _fake_list(stdout: str, returncode: int = 0):
    def _run(cmd, capture_output=True, text=True, timeout=30):
        return subprocess.CompletedProcess(cmd, returncode, stdout, "")
    return _run


def test_main_iids_outputs_bare_numbers(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["prs.py", "author=@me,iids"])
    monkeypatch.setattr(prs.subprocess, "run", _fake_list('[{"number": 10}, {"number": 20}]'))
    rc = prs.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert out.split() == ["10", "20"]


def test_main_iids_skips_missing_number(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["prs.py", "iids"])
    monkeypatch.setattr(prs.subprocess, "run", _fake_list('[{"number": 10}, {"title": "no number"}]'))
    prs.main()
    out = capsys.readouterr().out
    assert "None" not in out
    assert out.split() == ["10"]


def test_main_failed_filters_to_failing(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["prs.py", "author=@me,failed,iids"])
    rollup_fail = [{"conclusion": "FAILURE", "name": "x"}]
    rollup_ok = [{"conclusion": "SUCCESS"}]
    payload = (
        '[{"number": 10, "statusCheckRollup": ' + str(rollup_fail).replace("'", '"') + '},'
        ' {"number": 20, "statusCheckRollup": ' + str(rollup_ok).replace("'", '"') + '}]'
    )
    monkeypatch.setattr(prs.subprocess, "run", _fake_list(payload))
    rc = prs.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert out.split() == ["10"]


def test_main_gh_error_returns_1(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["prs.py", ""])
    monkeypatch.setattr(prs.subprocess, "run", _fake_list("", returncode=1))
    assert prs.main() == 1


def test_main_bad_json_returns_1(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["prs.py", "nopipe"])
    monkeypatch.setattr(prs.subprocess, "run", _fake_list("not json"))
    assert prs.main() == 1
