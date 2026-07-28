"""Unit tests for presets/github/job.py — :raw mode and line slicing."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

PRESET_PATH = Path(__file__).parent.parent / "presets" / "github" / "job.py"
_spec = importlib.util.spec_from_file_location("github_job", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
job = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(job)


def _make_fake_run(trace_lines: list[str], conclusion: str = "failure"):
    """Fake subprocess.run handling gh metadata + log endpoints.

    gh CLI calls in github/job.py:
      - `gh api repos/.../actions/jobs/{id}`        — job metadata
      - `gh run view {id} --json ...`               — run/PR refs
      - `gh pr view {n} --json ...`                 — PR details
      - `gh api repos/.../actions/jobs/{id}/logs`   — log
    """
    trace = "\n".join(trace_lines) + "\n"
    meta = json.dumps({
        "name": "test-job",
        "status": "completed",
        "conclusion": conclusion,
        "run_id": 42,
        "run_url": "https://github.com/x/y/actions/runs/42",
    })

    def fake_run(args: list[str], **kw: Any) -> subprocess.CompletedProcess:
        cmd = args[1] if len(args) > 1 else ""
        url = args[2] if len(args) > 2 else ""
        if cmd == "api" and url.endswith("/logs"):
            stdout = trace
        elif cmd == "api":
            stdout = meta
        else:
            # Skip run view / pr view — return empty JSON, non-fatal
            return subprocess.CompletedProcess(
                args=args, returncode=1, stdout="", stderr=""
            )
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=stdout, stderr=""
        )

    return fake_run


def _run_main(
    monkeypatch, argv: list[str], trace_lines: list[str], conclusion: str = "failure"
) -> int:
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(job.subprocess, "run", _make_fake_run(trace_lines, conclusion))
    return job.main()


# Trimmed, timestamp-stripped excerpt of a real failed Actions run in this repo
# (job 90097563482, run 30302191794, `pytest (windows-latest, 3.9)`) — a CRLF
# assertion failure with no build/setup errors around it. Used to pin the
# unmatched-failure banner against a real log shape rather than a synthetic
# four-liner.
REAL_FAILED_JOB_EXCERPT = [
    "........................................................................ [ 88%]",
    "sss.......ssss....s                                                      [100%]",
    "=========================== short test summary info ===========================",
    "FAILED tests/test_git_state_guard.py::test_snapshot_names_nested_refs_by_their_branch_name"
    " - AssertionError: assert b'eeeeeeeeeee...eeeeeeeee\\r\\n' == b'eeeeeeeeeee...eeeeeeeeeee\\n'",
    "  At index 40 diff: b'\\r' != b'\\n'",
    "1 failed, 3866 passed, 227 skipped in 93.77s (0:01:33)",
    "##[error]Process completed with exit code 1.",
]


def test_raw_dumps_full_trace(monkeypatch, capsys) -> None:
    lines = [f"line{i}" for i in range(1, 11)]
    rc = _run_main(monkeypatch, ["job.py", "123", "raw"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "## Raw lines 1-10 of 10" in out
    assert "    1 | line1" in out
    assert "   10 | line10" in out
    assert "Error context" not in out
    assert "Tail (last" not in out


def test_raw_slice_with_start_and_end(monkeypatch, capsys) -> None:
    lines = [f"line{i}" for i in range(1, 21)]
    rc = _run_main(monkeypatch, ["job.py", "123", "raw", "5", "8"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "## Raw lines 5-8 of 20" in out
    assert "    5 | line5" in out
    assert "    8 | line8" in out
    assert "line4" not in out
    assert "line9" not in out


def test_raw_slice_with_start_only_runs_to_end(monkeypatch, capsys) -> None:
    lines = [f"line{i}" for i in range(1, 11)]
    rc = _run_main(monkeypatch, ["job.py", "123", "raw", "7"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "## Raw lines 7-10 of 10" in out
    assert "    7 | line7" in out
    assert "   10 | line10" in out
    assert "line6" not in out


def test_raw_start_beyond_total_returns_nothing(monkeypatch, capsys) -> None:
    lines = [f"line{i}" for i in range(1, 6)]
    rc = _run_main(monkeypatch, ["job.py", "123", "raw", "100"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "start (100) > total (5)" in out


def test_raw_end_clamped_to_total(monkeypatch, capsys) -> None:
    lines = [f"line{i}" for i in range(1, 6)]
    rc = _run_main(monkeypatch, ["job.py", "123", "raw", "3", "999"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "## Raw lines 3-5 of 5" in out


def test_raw_invalid_start_returns_error(monkeypatch, capsys) -> None:
    rc = _run_main(monkeypatch, ["job.py", "123", "raw", "abc"], ["x"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "START/END must be integers" in out


def test_default_mode_runs_smart_filter(monkeypatch, capsys) -> None:
    lines = ["build started", "ERROR: thing exploded", "build done"]
    rc = _run_main(monkeypatch, ["job.py", "123"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Raw lines" not in out
    assert "Log: 3 lines total" in out


def test_fail_mode_shows_all_matched_blocks(monkeypatch, capsys) -> None:
    """`:fail` shows every error block with no tail truncation."""
    lines = ["build start"] + [f"line {i}" for i in range(150)] + [
        "##[error]Process completed with exit code 1",
    ] + [f"trail {i}" for i in range(40)]
    rc = _run_main(monkeypatch, ["job.py", "123", "fail"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "All error blocks" in out
    assert "exit code 1" in out
    assert "Tail (last" not in out


def test_fail_mode_no_matches(monkeypatch, capsys) -> None:
    """`:fail` on a failed job with zero matches is a tool gap, and must read as one.

    This fixture's conclusion is "failure" (the `_run_main` default) — the old
    `## No error patterns matched` wording was pinning the exact lie #453 files
    against: a crashed job rendering as green.
    """
    lines = ["build started", "all good", "build done"]
    rc = _run_main(monkeypatch, ["job.py", "123", "fail"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "## FAILED — no error pattern matched" in out
    assert "not that the log is clean" in out
    assert "No error patterns matched" not in out


def test_fail_is_alias_of_errors(monkeypatch, capsys) -> None:
    """`:fail` produces byte-identical output to `:errors` — pure alias."""
    lines = ["build start"] + [f"line {i}" for i in range(150)] + [
        "##[error]Process completed with exit code 1",
    ] + [f"trail {i}" for i in range(40)]
    _run_main(monkeypatch, ["job.py", "123", "errors"], lines)
    errors_out = capsys.readouterr().out
    _run_main(monkeypatch, ["job.py", "123", "fail"], lines)
    fail_out = capsys.readouterr().out
    assert fail_out == errors_out
    assert "All error blocks" in fail_out


def test_default_mode_unmatched_failure_shows_banner(monkeypatch, capsys) -> None:
    """A failed job the configured patterns can't classify gets the banner, not silence.

    Real log excerpt (see REAL_FAILED_JOB_EXCERPT) from a genuinely failed
    Actions run in this repo, with error_patterns narrowed to something absent
    from it — the realistic shape of a job_patterns table tuned for a
    different job. Zero matches on a job GitHub calls `failure` must never
    read as "nothing wrong" (#453, mirrors #445/#452 on the GitLab side).
    """
    monkeypatch.setenv("SUPERTOOL_ERROR_PATTERNS", "ZZZ_NEVER_APPEARS_IN_THIS_LOG")
    rc = _run_main(monkeypatch, ["job.py", "123"], REAL_FAILED_JOB_EXCERPT)
    out = capsys.readouterr().out
    assert rc == 0
    assert "## FAILED — no error pattern matched" in out
    assert "Job status is `failure`" in out
    assert "not that the log is clean" in out
    assert "Patterns tried: ZZZ_NEVER_APPEARS_IN_THIS_LOG" in out
    assert "Log tail (last" in out
    assert "AssertionError" in out  # the actual evidence still reaches the reader
    assert "gh-job:123:raw" in out
    assert "No error patterns matched" not in out


def test_default_mode_unmatched_non_failure_keeps_old_behavior(monkeypatch, capsys) -> None:
    """A job that did not fail and matches nothing keeps the old, honest silent tail.

    The default view never printed a textual banner here (only `:errors`/`:fail`
    did) — mirrors gl-job's own default-view contract. The banner is false to
    print when nothing actually went wrong, so it must not appear.
    """
    lines = ["build started", "all good", "build done"]
    rc = _run_main(monkeypatch, ["job.py", "123"], lines, conclusion="success")
    out = capsys.readouterr().out
    assert rc == 0
    assert "FAILED — no error pattern matched" not in out
    assert "No error patterns matched" not in out
    assert "build started" in out
    assert "all good" in out


def test_errors_mode_unmatched_non_failure_keeps_old_wording(monkeypatch, capsys) -> None:
    """Same invariant under `:errors`/`:fail` — only a real failure gets the banner."""
    lines = ["build started", "all good", "build done"]
    rc = _run_main(monkeypatch, ["job.py", "123", "errors"], lines, conclusion="success")
    out = capsys.readouterr().out
    assert rc == 0
    assert "No error patterns matched" in out
    assert "FAILED — no error pattern matched" not in out


def test_grep_matches_with_context(monkeypatch, capsys) -> None:
    """grep mode returns matching lines plus surrounding context."""
    lines = [f"line {i}" for i in range(20)] + [
        "##[error]Process completed with exit code 1",
    ] + [f"trail {i}" for i in range(20)]
    rc = _run_main(monkeypatch, ["job.py", "123", "grep", "exit code"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "grep /exit code/ — 1 matching lines" in out
    assert "exit code 1" in out
    assert "Raw lines" not in out


def test_grep_no_match_falls_back_to_tail(monkeypatch, capsys) -> None:
    """No match never returns empty — names the pattern and shows a tail."""
    lines = [f"line {i}" for i in range(100)]
    rc = _run_main(monkeypatch, ["job.py", "123", "grep", "zzz_nonexistent"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "No lines match /zzz_nonexistent/ (searched 100 lines)" in out
    assert "Showing last" in out
    assert "fallback" in out


def test_grep_bad_regex_falls_back_to_literal(monkeypatch, capsys) -> None:
    """An invalid regex is matched literally instead of crashing."""
    lines = ["before", "1) FooTest::testBar", "after"]
    rc = _run_main(monkeypatch, ["job.py", "123", "grep", "1)"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "(literal match)" in out
    assert "regex failed to compile" not in out
    assert "1) FooTest::testBar" in out


def test_grep_missing_pattern_returns_error(monkeypatch, capsys) -> None:
    """grep with no pattern is a usage error."""
    rc = _run_main(monkeypatch, ["job.py", "123", "grep"], ["x"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "usage: gh-job:JOB_ID:grep:PATTERN" in out


def test_select_job_patterns_matches_by_name() -> None:
    table = [{"job": "build", "patterns": ["##[error]"], "resolution": "rerun:{id}"}]
    patterns, resolution = job._select_job_patterns("build", table, ["DEFAULT"])
    assert patterns == ["##[error]"]
    assert resolution == "rerun:{id}"


def test_parse_job_patterns_malformed_returns_empty() -> None:
    assert job._parse_job_patterns("not json") == []
    assert job._parse_job_patterns("") == []


def test_job_patterns_shows_resolution_with_interpolated_id(monkeypatch, capsys) -> None:
    """A matched job prints its resolution op with {id} replaced by the job id."""
    monkeypatch.setenv("SUPERTOOL_JOB_PATTERNS", json.dumps([
        {"job": "test-job", "patterns": ["BOOM"], "resolution": "rerun:{id}"}
    ]))
    lines = ["noise", "BOOM goes the build", "noise"]
    rc = _run_main(monkeypatch, ["job.py", "555"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "BOOM goes the build" in out
    assert "Resolve:  ./supertool 'rerun:555'" in out
