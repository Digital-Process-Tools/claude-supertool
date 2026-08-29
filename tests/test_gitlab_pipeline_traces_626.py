"""Tests for gl-pipeline:ID:traces — write every failed job's trace (#626).

The pipeline is the actual entry point ("pipeline failed"), not a job id —
this mode reuses `gl-pipeline:ID:failed`'s job list and hands the ids to
`gl-job`'s own trace writer, so there is one trace-writing implementation.
"""
from __future__ import annotations

import importlib.util
import json
import re
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


# ---------------------------------------------------------------------------
# #2103 — a hostile job id must not reach the filesystem path write_traces
# builds, even through this route, which validates nothing of its own before
# handing the id list straight to write_traces. `_untrusted.flat` (used at
# line ~197 for the ids passed into write_traces) is a display neutraliser,
# not a path check -- it leaves a traversal string byte-for-byte unchanged.
# These tests do NOT stub out write_traces the way every test above does:
# stubbing it is exactly what let this route ship with no test ever reaching
# the real filename construction.
# ---------------------------------------------------------------------------

def _jobs_with_id(job_id, status: str = "failed") -> str:
    return json.dumps([{
        "name": "job_0", "stage": "test", "status": status, "duration": 5.0,
        "id": job_id, "web_url": "https://gl/job/x",
        "pipeline": {"status": "running"},
    }])


def _fake_run_list_and_job(list_payload: str, jobs: dict):
    """Fake `subprocess.run` that answers both the pipeline job-list call
    (`projects/:id/pipelines/N/jobs`) and gl-job's own per-id meta/trace
    calls (`projects/:id/jobs/ID[/trace]`), keyed by URL shape rather than
    by call order, since `pipe.subprocess` and `job.subprocess` are the same
    stdlib module object and both routes go through this one fake.

    Matched by literal URL suffix against each known id, not by extracting
    the id out of the URL with a regex -- a traversal id contains its own
    `/`, so `/jobs/([^/]+)(/trace)?$` never matches it and a regex-extraction
    fake would silently fall through to "not found" for exactly the id this
    test exists to exercise, proving nothing about the real vulnerability.
    """
    def fake_run(args, **kw):
        url = args[2] if len(args) > 2 else ""
        if re.search(r"/pipelines/\d+/jobs$", url):
            return _Result(list_payload, "", 0)
        for job_id, info in jobs.items():
            if url.endswith(f"/jobs/{job_id}/trace"):
                trace = info.get("trace", "")
                if isinstance(trace, list):
                    trace = "\n".join(trace) + "\n"
                return _Result(trace, info.get("trace_stderr", ""),
                                info.get("trace_returncode", 0))
            if url.endswith(f"/jobs/{job_id}"):
                meta = json.dumps({"name": info.get("name", "job"),
                                    "status": info.get("status", "failed")})
                return _Result(meta, "", 0)
        return _Result("", "not found", 1)
    return fake_run


def test_pipeline_traces_refuses_a_traversal_job_id_before_it_reaches_a_path(
        monkeypatch, tmp_path, capsys) -> None:
    """A malicious/compromised GitLab server can return whatever it wants in
    the job-listing response, including a forged `id` -- and, since the same
    server also answers the meta/trace fetches keyed by that id, a real trace
    fetch for a traversal id can genuinely succeed. The fake below simulates
    exactly that: the hostile id's own trace fetch returns real content, so
    an unpatched `write_traces` would actually write it to the path the id
    was built to reach -- one level above the traces directory here.
    """
    hostile_id = "../escaped-via-2103"
    payload = _jobs_with_id(hostile_id)
    jobs = {hostile_id: {"trace": ["ATTACKER-CONTROLLED-CONTENT"], "status": "failed"}}
    root = tmp_path / "root"
    monkeypatch.setattr(pipe.gitlab_job._image_root, "default_root",
                         lambda suffix="": str(root))
    monkeypatch.setattr(pipe.subprocess, "run",
                         _fake_run_list_and_job(payload, jobs))
    monkeypatch.setattr(sys, "argv", ["pipeline.py", "42", "traces"])
    rc = pipe.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "numeric" in out.lower()
    # The path the traversal id was built to reach -- one directory above
    # traces/ -- must not exist, and must not carry the attacker's content.
    escaped = root / "escaped-via-2103.log"
    assert not escaped.exists()
    written = list((root / "traces").glob("*.log")) if (root / "traces").exists() else []
    assert written == []


def test_pipeline_traces_with_an_ordinary_numeric_id_still_writes(
        monkeypatch, tmp_path, capsys) -> None:
    """The positive control: refusing a hostile id must not also refuse or
    otherwise break the ordinary case this route exists to serve."""
    payload = _jobs_with_id("777")
    jobs = {"777": {"trace": ["real-log-line"], "status": "failed"}}
    root = tmp_path / "root"
    monkeypatch.setattr(pipe.gitlab_job._image_root, "default_root",
                         lambda suffix="": str(root))
    monkeypatch.setattr(pipe.subprocess, "run",
                         _fake_run_list_and_job(payload, jobs))
    monkeypatch.setattr(sys, "argv", ["pipeline.py", "42", "traces"])
    rc = pipe.main()
    out = capsys.readouterr().out
    assert rc == 0
    written = list((root / "traces").glob("*.log"))
    assert len(written) == 1
    assert written[0].name == "job-777.log"
    assert "real-log-line" in written[0].read_text(encoding="utf-8")
    assert "[trace]" in out
