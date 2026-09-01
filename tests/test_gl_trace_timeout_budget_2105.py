"""#2105 — `gl-job:trace` / `gl-pipeline:traces` can spend more subprocess
budget per call than their own declared op timeout allows for, so a trace
already fetched is discarded at the kill with no file and no receipt.

`_fetch_trace_and_meta` spends up to 10s (metadata) + 20s (trace) = 30s per
job id, serially, inside `write_traces`. Against that:

- `gl-job` declared a 30s timeout while advertising a comma-separated
  multi-id `:trace` form (#2095) — two ids alone exceed the budget.
- `gl-pipeline` declared 15s for `:traces`, which fans out over every failed
  job in the pipeline (#626) — unbounded from the caller's side, since the
  ids come from GitLab's own job listing rather than something typed.

The fix caps how many ids `write_traces` will actually fetch in one call
(`GL_JOB_TRACE_MAX_IDS`, default 6 — already the threshold the filename
logic uses for "a pipeline with dozens of failed jobs") and raises both
ops' declared timeouts to cover that many ids serially with margin. A
request over the cap is not silently trimmed: the ids beyond it are named
as not-fetched, with the knob that raises the limit, so a caller controlling
a small comma-separated list has no reason to hit it and a fan-out from
`gl-pipeline` says exactly what was left out rather than being killed
mid-fetch with nothing to show for the ids already done.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest import mock

_PRESETS = Path(__file__).parent.parent / "presets"


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, _PRESETS / relpath)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gl_job = _load("gitlab_job_2105", "gitlab/job.py")

_CONFIG = json.loads((_PRESETS / "gitlab.json").read_text())
_PER_ID_SECONDS = 10 + 20  # metadata timeout + trace timeout, per id, serial


def _declared_timeout(op: str) -> int:
    return _CONFIG["ops"][op]["timeout"]


# ---------------------------------------------------------------------------
# the declared budget actually covers the capped worst case
# ---------------------------------------------------------------------------

def test_gl_job_timeout_covers_the_capped_id_count() -> None:
    cap = gl_job.MAX_TRACE_IDS
    worst_case = cap * _PER_ID_SECONDS
    assert _declared_timeout("gl-job") > worst_case, (
        f"gl-job timeout {_declared_timeout('gl-job')}s does not cover "
        f"{cap} ids at {_PER_ID_SECONDS}s each ({worst_case}s) — a full "
        f"multi-id :trace call at the cap would be killed before finishing"
    )


def test_gl_pipeline_timeout_covers_its_own_fetch_plus_the_capped_write(monkeypatch) -> None:
    cap = gl_job.MAX_TRACE_IDS
    # gl-pipeline's own jobs-listing subprocess call, ahead of write_traces.
    pipeline_own_fetch = 15
    worst_case = pipeline_own_fetch + cap * _PER_ID_SECONDS
    assert _declared_timeout("gl-pipeline") > worst_case, (
        f"gl-pipeline timeout {_declared_timeout('gl-pipeline')}s does not "
        f"cover its own {pipeline_own_fetch}s fetch plus {cap} ids at "
        f"{_PER_ID_SECONDS}s each ({worst_case}s)"
    )


# ---------------------------------------------------------------------------
# over the cap: named, not silently dropped
# ---------------------------------------------------------------------------

def _fake_fetch(job_id: str):
    return f"trace for {job_id}\n", {"name": "job", "status": "failed"}, ""


def test_write_traces_over_the_cap_names_what_it_did_not_fetch(monkeypatch, capsys, tmp_path) -> None:
    monkeypatch.setattr(gl_job, "_fetch_trace_and_meta", _fake_fetch)
    monkeypatch.setattr(gl_job._image_root, "default_root", lambda suffix: str(tmp_path))
    monkeypatch.setattr(gl_job._image_root, "ensure", lambda p: (p, ""))

    cap = gl_job.MAX_TRACE_IDS
    ids = [str(i) for i in range(1, cap + 5)]  # four over the cap
    gl_job.write_traces(ids)
    out = capsys.readouterr().out

    assert f"GL_JOB_TRACE_MAX_IDS" in out, out
    for skipped_id in ids[cap:]:
        assert skipped_id in out, (
            f"id {skipped_id!r} is over the cap and must be named as "
            f"not-fetched, not silently dropped — {out}")


def test_write_traces_at_or_under_the_cap_fetches_everything_and_says_nothing_was_skipped(
    monkeypatch, capsys, tmp_path
) -> None:
    monkeypatch.setattr(gl_job, "_fetch_trace_and_meta", _fake_fetch)
    monkeypatch.setattr(gl_job._image_root, "default_root", lambda suffix: str(tmp_path))
    monkeypatch.setattr(gl_job._image_root, "ensure", lambda p: (p, ""))

    cap = gl_job.MAX_TRACE_IDS
    ids = [str(i) for i in range(1, cap + 1)]  # exactly at the cap
    gl_job.write_traces(ids)
    out = capsys.readouterr().out

    assert "GL_JOB_TRACE_MAX_IDS" not in out, (
        "a request at the cap must not print a truncation note it did not "
        f"apply — {out}")
