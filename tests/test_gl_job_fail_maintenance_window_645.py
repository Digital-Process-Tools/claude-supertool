"""gl-job:ID:fail annotates a boilerplate-only container exit against a
declared maintenance window (#645). Filed after five container deaths
across four MRs were misdiagnosed as "the fleet degrading under load" — the
proof they were benign only arrived hours later. `gl-job` used to report a
bare `exit code 137`/`143` indistinguishable from real OOM.

Every test below fails on the code as it stood before this change: no
maintenance-window annotation existed at all, so none of these strings
could appear in the output.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

PRESETS = Path(__file__).parent.parent / "presets"


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, PRESETS / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gl_job = _load("gitlab/job.py", "gitlab_job_maintenance_645")

_REAL_RUN = subprocess.run
GL_ID = "7125001"

LOG_BOILERPLATE_ONLY = "\n".join([
    "Running with gitlab-runner 17.1.0",
    "section_start:1750000000:prepare_executor",
    "Preparing environment",
    "section_end:1750000002:prepare_executor",
    "$ php vendor/bin/phpunit",
    "OK (42 tests, 100 assertions)",
    "section_start:1750000100:cleanup_file_variables",
    "Cleaning up project directory and file based variables",
    "section_end:1750000101:cleanup_file_variables",
    "ERROR: Job failed: exit code 137",
])

# exit 1 — a real test failure, never to be softened by a declared window.
LOG_REAL_FAILURE = "\n".join([
    "Running with gitlab-runner 17.1.0",
    "$ php vendor/bin/phpunit",
    "There was 1 failure:",
    "1) SomeTest::test_thing",
    "Failed asserting that false is true.",
    "FAILURES!",
    "Tests: 1, Assertions: 1, Failures: 1.",
    "ERROR: Job failed: exit code 1",
])


def _gl_meta(finished_at, runner_description, runner_id) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "id": int(GL_ID),
        "name": "dv_test",
        "status": "failed",
        "stage": "test",
        "duration": 12.0,
        "web_url": f"https://gitlab.example/-/jobs/{GL_ID}",
        "ref": "",
        "pipeline": {"id": 999},
        "finished_at": finished_at,
    }
    if runner_description is not None or runner_id is not None:
        meta["runner"] = {"description": runner_description, "id": runner_id}
    return meta


def _fake_glab(meta: dict[str, Any], log: str):
    def fake_run(args: list[str], **kw: Any) -> subprocess.CompletedProcess:
        if args and args[0] == "git":
            return _REAL_RUN(args, **kw)
        assert args and args[0] == "glab", f"unstubbed command: {args!r}"
        url = args[2] if len(args) > 2 else ""
        if url.endswith("/trace"):
            return subprocess.CompletedProcess(args, 0, log, "")
        if "/jobs/" in url:
            return subprocess.CompletedProcess(args, 0, json.dumps(meta), "")
        raise AssertionError(f"unstubbed glab call: {args!r}")
    return fake_run


def _run(monkeypatch, capsys, cfg, log, finished_at=None,
          runner_description=None, runner_id=None) -> str:
    monkeypatch.setattr(sys, "argv", ["job.py", GL_ID, "fail"])
    meta = _gl_meta(finished_at, runner_description, runner_id)
    monkeypatch.setattr(gl_job.subprocess, "run", _fake_glab(meta, log))
    monkeypatch.setattr(gl_job._maintenance, "load_config", lambda: cfg)
    gl_job.main()
    return capsys.readouterr().out


FLEET_CFG = {
    "gl-runners": {
        "maintenance": {"window": "00:00 UTC", "duration": "5m"},
    }
}


def test_container_exit_inside_declared_window_gets_a_retry_verdict(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    out = _run(monkeypatch, capsys, FLEET_CFG, LOG_BOILERPLATE_ONLY,
               finished_at="2026-07-31T00:00:08Z",
               runner_description="dptools-runner-1", runner_id=1)
    assert "inside the declared maintenance window" in out
    assert "RETRY" in out


def test_container_exit_outside_declared_window_is_named_but_not_cleared(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    out = _run(monkeypatch, capsys, FLEET_CFG, LOG_BOILERPLATE_ONLY,
               finished_at="2026-07-31T06:00:00Z",
               runner_description="dptools-runner-1", runner_id=1)
    assert "outside the declared maintenance window" in out
    assert "RETRY" not in out


def test_no_window_declared_leaves_output_unchanged_from_before_645(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    out = _run(monkeypatch, capsys, {}, LOG_BOILERPLATE_ONLY,
               finished_at="2026-07-31T00:00:08Z",
               runner_description="dptools-runner-1", runner_id=1)
    assert "maintenance window" not in out.lower()
    assert gl_job.BOILERPLATE_ONLY_HEADER in out


def test_malformed_window_config_reads_as_declared_but_unparseable_never_an_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Positive control pairs with the well-formed case above: a garbled
    declaration must not crash gl-job, and must not fabricate a RETRY/outside
    verdict -- but it also must not render byte-identical to "no window
    declared at all" (self-review finding), since that would hide from the
    operator that their own config never took effect."""
    broken_cfg = {"gl-runners": {"maintenance": {"window": "midnight-ish", "duration": "5m"}}}
    out = _run(monkeypatch, capsys, broken_cfg, LOG_BOILERPLATE_ONLY,
               finished_at="2026-07-31T00:00:08Z",
               runner_description="dptools-runner-1", runner_id=1)
    assert "could not be parsed" in out
    assert "RETRY" not in out
    assert "inside the declared maintenance window" not in out
    assert "outside the declared maintenance window" not in out
    assert gl_job.BOILERPLATE_ONLY_HEADER in out


def test_a_real_test_failure_is_never_softened_by_a_declared_window(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Scope guard: exit code 1 at 00:00:03 UTC is still a PHPUnit failure."""
    out = _run(monkeypatch, capsys, FLEET_CFG, LOG_REAL_FAILURE,
               finished_at="2026-07-31T00:00:03Z",
               runner_description="dptools-runner-1", runner_id=1)
    assert "maintenance window" not in out.lower()
    assert "RETRY" not in out
