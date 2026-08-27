"""Tests for gl-job:trace — write the full CI trace to disk (#626).

`gl-job:ID:grep:PATTERN` truncates and `:raw` floods context; this mode
writes the whole trace to a file the caller can then read with full
fidelity through the ordinary read ops, and prints only the path plus a
short summary.
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

PRESET_PATH = Path(__file__).parent.parent / "presets" / "gitlab" / "job.py"
_spec = importlib.util.spec_from_file_location("gitlab_job_trace", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
job = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(job)


def _make_fake_multi_run(jobs: dict[str, dict]):
    """Fake `subprocess.run` keyed by job id, parsed out of the glab URL.

    `jobs[id]` holds `trace` (str or list[str]), `status`, `name`, and
    optionally `meta_returncode` / `trace_returncode` / `trace_stderr` to
    simulate a fetch failure for that one id without touching the others.
    """
    def fake_run(args: list[str], **kw: Any) -> subprocess.CompletedProcess:
        url = args[2] if len(args) > 2 else ""
        m = re.search(r"/jobs/(\d+)(/trace)?$", url)
        job_id = m.group(1) if m else "?"
        info = jobs.get(job_id, {})
        if url.endswith("/trace"):
            trace = info.get("trace", "")
            if isinstance(trace, list):
                trace = "\n".join(trace) + "\n"
            return subprocess.CompletedProcess(
                args=args,
                returncode=info.get("trace_returncode", 0),
                stdout=trace,
                stderr=info.get("trace_stderr", ""),
            )
        meta = json.dumps({
            "name": info.get("name", "test-job"),
            "status": info.get("status", "failed"),
            "stage": "test",
            "duration": 12.0,
            "web_url": f"https://gitlab.example/job/{job_id}",
            "ref": "feature/x",
            "pipeline": {"id": 999},
        })
        return subprocess.CompletedProcess(
            args=args, returncode=info.get("meta_returncode", 0),
            stdout=meta, stderr="",
        )
    return fake_run


def _run_trace(monkeypatch, tmp_path, argv, jobs):
    root = tmp_path / "supertool-images-test"
    monkeypatch.setattr(job._image_root, "default_root", lambda suffix="": str(root))
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(job.subprocess, "run", _make_fake_multi_run(jobs))
    rc = job.main()
    return rc, root


# ---------------------------------------------------------------------------
# base case — one job id
# ---------------------------------------------------------------------------

def test_trace_writes_full_log_and_prints_path_and_receipt(monkeypatch, tmp_path, capsys) -> None:
    lines = [f"line{i}" for i in range(1, 21)]
    rc, root = _run_trace(monkeypatch, tmp_path, ["job.py", "123", "trace"],
                          {"123": {"trace": lines, "status": "failed"}})
    out = capsys.readouterr().out
    assert rc == 0
    assert "[trace]" in out
    assert "20 lines" in out
    assert "->" in out

    written = list((root / "traces").glob("*.log"))
    assert len(written) == 1
    content = written[0].read_text()
    for line in lines:
        assert line in content
    assert str(written[0]) in out


def test_trace_does_not_truncate_a_large_log(monkeypatch, tmp_path, capsys) -> None:
    """The whole point: :raw and :grep both cap output; :trace must not."""
    lines = [f"line{i}" for i in range(1, 6001)]  # well past GL_JOB_RAW_MAX_LINES
    rc, root = _run_trace(monkeypatch, tmp_path, ["job.py", "9", "trace"],
                          {"9": {"trace": lines, "status": "failed"}})
    assert rc == 0
    content = (root / "traces").glob("*.log")
    text = next(iter(content)).read_text()
    assert "line1\n" in text or text.startswith("line1")
    assert "line6000" in text
    assert text.count("\n") >= 6000


def test_trace_rejects_non_numeric_job_id(monkeypatch, tmp_path, capsys) -> None:
    rc, _ = _run_trace(monkeypatch, tmp_path, ["job.py", "abc", "trace"], {})
    out = capsys.readouterr().out
    assert rc == 1
    assert "numeric job id" in out


def test_trace_rejects_extra_argv_after_mode(monkeypatch, tmp_path, capsys) -> None:
    rc, _ = _run_trace(monkeypatch, tmp_path, ["job.py", "123", "trace", "bogus"], {})
    out = capsys.readouterr().out
    assert rc == 1
    assert "trace" in out.lower()


# ---------------------------------------------------------------------------
# multiple job ids (#626 refinement 1)
# ---------------------------------------------------------------------------

def test_trace_multiple_ids_concatenates_into_one_file(monkeypatch, tmp_path, capsys) -> None:
    jobs = {
        "111": {"trace": ["alpha-line-one", "alpha-line-two"], "status": "failed", "name": "job-a"},
        "222": {"trace": ["beta-line-one", "beta-line-two"], "status": "failed", "name": "job-b"},
    }
    rc, root = _run_trace(monkeypatch, tmp_path, ["job.py", "111,222", "trace"], jobs)
    out = capsys.readouterr().out
    assert rc == 0
    written = list((root / "traces").glob("*.log"))
    assert len(written) == 1
    content = written[0].read_text()
    assert "alpha-line-one" in content
    assert "beta-line-one" in content
    assert "111" in content and "222" in content
    assert "[trace]" in out


def test_trace_rejects_a_non_numeric_id_in_the_list(monkeypatch, tmp_path, capsys) -> None:
    rc, _ = _run_trace(monkeypatch, tmp_path, ["job.py", "111,abc", "trace"], {})
    out = capsys.readouterr().out
    assert rc == 1
    assert "numeric" in out


def test_trace_rejects_an_empty_piece_in_the_list(monkeypatch, tmp_path, capsys) -> None:
    rc, _ = _run_trace(monkeypatch, tmp_path, ["job.py", "111,,222", "trace"], {})
    out = capsys.readouterr().out
    assert rc == 1


def test_trace_dedupes_a_repeated_id(monkeypatch, tmp_path, capsys) -> None:
    calls = {"trace": 0}
    jobs = {"111": {"trace": ["only-line"], "status": "failed"}}
    fake = _make_fake_multi_run(jobs)

    def counting_fake(args, **kw):
        if len(args) > 2 and args[2].endswith("/trace"):
            calls["trace"] += 1
        return fake(args, **kw)

    monkeypatch.setattr(job._image_root, "default_root",
                         lambda suffix="": str(tmp_path / "root"))
    monkeypatch.setattr(sys, "argv", ["job.py", "111,111", "trace"])
    monkeypatch.setattr(job.subprocess, "run", counting_fake)
    rc = job.main()
    assert rc == 0
    assert calls["trace"] == 1


# ---------------------------------------------------------------------------
# file-already-exists / empty-trace / partial-failure — three states, not two
# ---------------------------------------------------------------------------

def test_trace_overwrites_an_existing_file_and_says_so(monkeypatch, tmp_path, capsys) -> None:
    jobs = {"123": {"trace": ["new-content-line"], "status": "failed"}}
    root = tmp_path / "root"
    (root / "traces").mkdir(parents=True)
    existing = root / "traces" / "job-123.log"
    existing.write_text("stale-old-content\n")

    monkeypatch.setattr(job._image_root, "default_root", lambda suffix="": str(root))
    monkeypatch.setattr(sys, "argv", ["job.py", "123", "trace"])
    monkeypatch.setattr(job.subprocess, "run", _make_fake_multi_run(jobs))
    rc = job.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "overwrote" in out.lower()
    content = existing.read_text()
    assert "new-content-line" in content
    assert "stale-old-content" not in content


def test_trace_empty_log_says_so_and_writes_nothing(monkeypatch, tmp_path, capsys) -> None:
    jobs = {"123": {"trace": "", "status": "pending"}}
    rc, root = _run_trace(monkeypatch, tmp_path, ["job.py", "123", "trace"], jobs)
    out = capsys.readouterr().out
    assert rc == 0
    assert "empty" in out.lower()
    written = list((root / "traces").glob("*.log")) if (root / "traces").exists() else []
    assert written == []


def test_trace_partial_fetch_failure_still_writes_the_others(monkeypatch, tmp_path, capsys) -> None:
    jobs = {
        "111": {"trace": ["good-line"], "status": "failed"},
        "222": {"trace": "", "status": "failed", "trace_returncode": 1,
                "trace_stderr": "HTTP 404 not found"},
    }
    rc, root = _run_trace(monkeypatch, tmp_path, ["job.py", "111,222", "trace"], jobs)
    out = capsys.readouterr().out
    assert rc == 0
    written = list((root / "traces").glob("*.log"))
    assert len(written) == 1
    content = written[0].read_text()
    assert "good-line" in content
    assert "222" in out
    assert "not found" in out.lower()


def test_trace_all_fetches_fail_returns_nonzero_and_writes_nothing(monkeypatch, tmp_path, capsys) -> None:
    jobs = {
        "111": {"trace": "", "trace_returncode": 1, "trace_stderr": "HTTP 404 not found"},
    }
    rc, root = _run_trace(monkeypatch, tmp_path, ["job.py", "111", "trace"], jobs)
    out = capsys.readouterr().out
    assert rc == 1
    assert "not found" in out.lower()
    assert not (root / "traces").exists() or list((root / "traces").glob("*.log")) == []


# ---------------------------------------------------------------------------
# summary — three states: computed / could-not-tell, never a guessed zero
# ---------------------------------------------------------------------------

def test_trace_summary_counts_when_phpunit_line_present(monkeypatch, tmp_path, capsys) -> None:
    lines = [
        "PHPUnit 9.5",
        "FAILURES!",
        "Tests: 10, Assertions: 20, Failures: 2, Errors: 0.",
    ]
    rc, root = _run_trace(monkeypatch, tmp_path, ["job.py", "123", "trace"],
                          {"123": {"trace": lines, "status": "failed"}})
    out = capsys.readouterr().out
    assert rc == 0
    assert "[failures] 2 failures, 0 errors" in out


def test_trace_summary_could_not_tell_when_no_recognisable_line(monkeypatch, tmp_path, capsys) -> None:
    lines = ["build started", "something broke", "build ended"]
    rc, root = _run_trace(monkeypatch, tmp_path, ["job.py", "123", "trace"],
                          {"123": {"trace": lines, "status": "failed"}})
    out = capsys.readouterr().out
    assert rc == 0
    assert "could not tell" in out.lower()
    # Must never render as a confident zero when nothing was actually counted.
    assert "0 failures, 0 errors" not in out


def test_trace_first_failure_line_when_present(monkeypatch, tmp_path, capsys) -> None:
    lines = [
        "build output",
        "1) SiClientMissionBoard\\Tests\\PageIndexDelegateTest::testBasic",
        "Failed asserting that false is true.",
    ]
    rc, root = _run_trace(monkeypatch, tmp_path, ["job.py", "123", "trace"],
                          {"123": {"trace": lines, "status": "failed"}})
    out = capsys.readouterr().out
    assert rc == 0
    assert "[first]" in out
    assert "PageIndexDelegateTest::testBasic" in out


def test_trace_no_first_line_claim_when_absent(monkeypatch, tmp_path, capsys) -> None:
    lines = ["build output", "generic failure, no PHPUnit block header here"]
    rc, root = _run_trace(monkeypatch, tmp_path, ["job.py", "123", "trace"],
                          {"123": {"trace": lines, "status": "failed"}})
    out = capsys.readouterr().out
    assert rc == 0
    assert "[first]" not in out
