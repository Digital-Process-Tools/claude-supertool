"""Unit tests for presets/gitlab/job.py — :raw mode and line slicing."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

PRESET_PATH = Path(__file__).parent.parent / "presets" / "gitlab" / "job.py"
_spec = importlib.util.spec_from_file_location("gitlab_job", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
job = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(job)


def _make_fake_run(trace_lines: list[str]):
    """Return a fake subprocess.run that handles glab metadata + trace calls."""
    trace = "\n".join(trace_lines) + "\n"
    meta = json.dumps({
        "name": "test-job",
        "status": "failed",
        "stage": "test",
        "duration": 12.0,
        "web_url": "https://gitlab.example/job/1",
        "ref": "feature/x",
        "pipeline": {"id": 999},
    })

    def fake_run(args: list[str], **kw: Any) -> subprocess.CompletedProcess:
        url = args[2] if len(args) > 2 else ""
        if url.endswith("/trace"):
            stdout = trace
        else:
            stdout = meta
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
    # No smart-mode artefacts
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
    """Sanity — without :raw, the smart filter path is taken."""
    lines = ["build started", "ERROR: thing exploded", "build done"]
    rc = _run_main(monkeypatch, ["job.py", "123"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Raw lines" not in out
    # Default path prints a header with job + log line count
    assert "Log: 3 lines total" in out


def test_errors_mode_shows_all_matched_blocks(monkeypatch, capsys) -> None:
    """errors mode shows every error block with no tail truncation."""
    # Simulate phpstan output: 200 lines with errors scattered
    lines = ["build start"] + [f"line {i}" for i in range(150)] + [
        " ------ ---- ",
        " Line   Dvsi/foo/Bar.class.php ",
        " 42     Type mismatch ",
        "    🪪  generics.notSubtype ",
        " ------ ---- ",
    ] + [f"trail {i}" for i in range(40)]
    rc = _run_main(monkeypatch, ["job.py", "123", "errors"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "All error blocks" in out
    # Should include the early-in-log error block, not just tail
    assert "generics.notSubtype" in out
    assert "Line   Dvsi/foo/Bar" in out


def test_errors_mode_no_matches(monkeypatch, capsys) -> None:
    """errors mode prints clear message when nothing matched."""
    lines = ["build started", "all good", "build done"]
    rc = _run_main(monkeypatch, ["job.py", "123", "errors"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "No error patterns matched" in out


def test_phpstan_identifier_marker_matches_default(monkeypatch, capsys) -> None:
    """🪪 marker (phpstan identifier) is in default patterns."""
    lines = [
        "noise",
        "noise",
        " 12  some phpstan error ",
        "    🪪  generics.notSubtype ",
        "noise",
    ]
    rc = _run_main(monkeypatch, ["job.py", "123"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    # The phpstan error should appear in error context section
    assert "generics.notSubtype" in out
    assert "Error context" in out
