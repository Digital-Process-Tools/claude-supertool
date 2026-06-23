"""Tests for the gl-pipeline op — filter modes and default bulk collapse.

Covers the pure error classifier and the three render modes (full / active /
failed) by monkeypatching subprocess.run, without hitting the live glab CLI.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

PRESET_PATH = Path(__file__).parent.parent / "presets" / "gitlab" / "pipeline.py"
_spec = importlib.util.spec_from_file_location("gitlab_pipeline", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
pipe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pipe)


class _Result:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _jobs(*statuses: str) -> str:
    """Build a glab jobs JSON payload, one job per status given."""
    out = []
    for i, status in enumerate(statuses):
        out.append({
            "name": f"job_{i}_{status}",
            "stage": "test",
            "status": status,
            "duration": 5.0,
            "id": 1000 + i,
            "web_url": f"https://gl/job/{1000 + i}",
            "pipeline": {"status": "running"},
        })
    return json.dumps(out)


def _patch(monkeypatch, payload: str, argv: list[str], returncode: int = 0,
           stderr: str = "") -> None:
    monkeypatch.setattr(
        pipe.subprocess, "run",
        lambda *a, **k: _Result(payload, stderr, returncode),
    )
    monkeypatch.setattr(sys, "argv", argv)


# ---------------------------------------------------------------------------
# _format_error
# ---------------------------------------------------------------------------

def test_format_error_404() -> None:
    msg = pipe._format_error("HTTP 404 not found", "Pipeline", "9")
    assert "not found" in msg


def test_format_error_401() -> None:
    msg = pipe._format_error("401 unauthorized", "Pipeline", "9")
    assert "glab auth login" in msg


def test_format_error_403() -> None:
    msg = pipe._format_error("403 forbidden", "Pipeline", "9")
    assert "permission denied" in msg


def test_format_error_generic() -> None:
    msg = pipe._format_error("boom", "Pipeline", "9")
    assert "glab failed" in msg


# ---------------------------------------------------------------------------
# argument validation
# ---------------------------------------------------------------------------

def test_no_args_is_usage_error(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["pipeline.py"])
    assert pipe.main() == 1
    assert "usage" in capsys.readouterr().out.lower()


def test_unknown_filter_errors(monkeypatch, capsys) -> None:
    _patch(monkeypatch, _jobs("success"), ["pipeline.py", "1", "bogus"])
    assert pipe.main() == 1
    out = capsys.readouterr().out
    assert "unknown filter" in out
    assert "'bogus'" in out


# ---------------------------------------------------------------------------
# full mode — bulk collapse
# ---------------------------------------------------------------------------

def test_full_collapses_noise_to_count(monkeypatch, capsys) -> None:
    payload = _jobs("success", "manual", "manual", "created", "skipped")
    _patch(monkeypatch, payload, ["pipeline.py", "42"])
    assert pipe.main() == 0
    out = capsys.readouterr().out
    # The one real job is shown as a row.
    assert "job_0_success" in out
    # The bulk is collapsed to a count line, not one row each.
    assert "job_1_manual" not in out
    assert "+2 manual" in out
    assert "+1 created" in out
    assert "+1 skipped" in out


def test_full_no_noise_prints_no_summary(monkeypatch, capsys) -> None:
    _patch(monkeypatch, _jobs("success", "failed"), ["pipeline.py", "42"])
    assert pipe.main() == 0
    out = capsys.readouterr().out
    assert "hidden" not in out


def test_full_lists_failed_detail(monkeypatch, capsys) -> None:
    _patch(monkeypatch, _jobs("success", "failed"), ["pipeline.py", "42"])
    pipe.main()
    out = capsys.readouterr().out
    assert "## Failed jobs (1)" in out
    assert "job #1001" in out


# ---------------------------------------------------------------------------
# active mode
# ---------------------------------------------------------------------------

def test_active_shows_only_running_pending(monkeypatch, capsys) -> None:
    payload = _jobs("success", "running", "pending", "manual", "failed")
    _patch(monkeypatch, payload, ["pipeline.py", "42", "active"])
    assert pipe.main() == 0
    out = capsys.readouterr().out
    assert "job_1_running" in out
    assert "job_2_pending" in out
    assert "job_0_success" not in out
    assert "job_4_failed" not in out


def test_active_none_prints_message(monkeypatch, capsys) -> None:
    _patch(monkeypatch, _jobs("success", "failed"), ["pipeline.py", "42", "active"])
    assert pipe.main() == 0
    assert "No running or pending jobs." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# failed mode
# ---------------------------------------------------------------------------

def test_failed_shows_only_failed_with_detail(monkeypatch, capsys) -> None:
    payload = _jobs("success", "failed", "running")
    _patch(monkeypatch, payload, ["pipeline.py", "42", "failed"])
    assert pipe.main() == 0
    out = capsys.readouterr().out
    assert "job_1_failed" in out
    assert "job_0_success" not in out
    assert "## Failed jobs (1)" in out
    assert "https://gl/job/1001" in out


def test_failed_none_prints_message(monkeypatch, capsys) -> None:
    _patch(monkeypatch, _jobs("success", "running"), ["pipeline.py", "42", "failed"])
    assert pipe.main() == 0
    assert "No failed jobs." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# error passthrough
# ---------------------------------------------------------------------------

def test_glab_error_returns_1(monkeypatch, capsys) -> None:
    _patch(monkeypatch, "", ["pipeline.py", "42"], returncode=1, stderr="404 not found")
    assert pipe.main() == 1
    assert "not found" in capsys.readouterr().out
