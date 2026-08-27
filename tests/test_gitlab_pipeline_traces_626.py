"""Tests for gl-pipeline:ID:traces — write every failed job's trace (#626).

The pipeline is the actual entry point ("pipeline failed"), not a job id —
this mode reuses `gl-pipeline:ID:failed`'s job list and hands the ids to
`gl-job`'s own trace writer, so there is one trace-writing implementation.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

PRESET_PATH = Path(__file__).parent.parent / "presets" / "gitlab" / "pipeline.py"
_spec = importlib.util.spec_from_file_location("gitlab_pipeline_traces", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
pipe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pipe)


class _Result:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _jobs(*statuses: str) -> str:
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


def test_pipeline_traces_mode_is_recognised(monkeypatch, capsys) -> None:
    """Not 'unknown filter' — this is the whole point of the mode existing."""
    monkeypatch.setattr(pipe.subprocess, "run",
                         lambda *a, **k: _Result(_jobs("success"), "", 0))
    monkeypatch.setattr(sys, "argv", ["pipeline.py", "42", "traces"])
    monkeypatch.setattr(pipe.gitlab_job, "write_traces", lambda ids: 0)
    rc = pipe.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "unknown filter" not in out


def test_pipeline_traces_calls_write_traces_with_failed_job_ids(monkeypatch, capsys) -> None:
    payload = _jobs("success", "failed", "failed", "running")
    monkeypatch.setattr(pipe.subprocess, "run", lambda *a, **k: _Result(payload, "", 0))
    monkeypatch.setattr(sys, "argv", ["pipeline.py", "42", "traces"])

    captured: dict = {}

    def fake_write_traces(ids):
        captured["ids"] = ids
        return 0

    monkeypatch.setattr(pipe.gitlab_job, "write_traces", fake_write_traces)
    rc = pipe.main()
    assert rc == 0
    assert captured["ids"] == ["1001", "1002"]


def test_pipeline_traces_no_failed_jobs_does_not_call_write_traces(monkeypatch, capsys) -> None:
    monkeypatch.setattr(pipe.subprocess, "run",
                         lambda *a, **k: _Result(_jobs("success", "running"), "", 0))
    monkeypatch.setattr(sys, "argv", ["pipeline.py", "42", "traces"])

    called = {"n": 0}
    monkeypatch.setattr(pipe.gitlab_job, "write_traces",
                         lambda ids: called.__setitem__("n", called["n"] + 1) or 0)
    rc = pipe.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "No failed jobs." in out
    assert called["n"] == 0


def test_pipeline_traces_propagates_write_traces_return_code(monkeypatch, capsys) -> None:
    payload = _jobs("failed")
    monkeypatch.setattr(pipe.subprocess, "run", lambda *a, **k: _Result(payload, "", 0))
    monkeypatch.setattr(sys, "argv", ["pipeline.py", "42", "traces"])
    monkeypatch.setattr(pipe.gitlab_job, "write_traces", lambda ids: 1)
    rc = pipe.main()
    assert rc == 1
