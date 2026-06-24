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


def _make_fake_run(trace_lines: list[str]):
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
        "conclusion": "failure",
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


def _run_main(monkeypatch, argv: list[str], trace_lines: list[str]) -> int:
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(job.subprocess, "run", _make_fake_run(trace_lines))
    return job.main()


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
