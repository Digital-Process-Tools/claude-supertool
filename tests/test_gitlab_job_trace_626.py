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

import pytest

PRESET_PATH = Path(__file__).parent.parent / "presets" / "gitlab" / "job.py"
_spec = importlib.util.spec_from_file_location("gitlab_job_trace", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
job = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(job)


@pytest.fixture(autouse=True)
def _trace_op_flag(monkeypatch):
    """#2146 split `:trace` mode into its own op, `gl-job-trace`, which sets
    SUPERTOOL_TRACE_OP=true so job.py's shared main() knows it was invoked
    through the op with the wider timeout budget rather than through `gl-job`
    itself. Every test in this file drives that trace dispatch directly via
    `job.main()`, standing in for a call routed through `gl-job-trace` --
    the refusal gl-job now prints without this flag is pinned separately in
    tests/test_gl_job_timeout_split_2146.py.
    """
    monkeypatch.setenv("SUPERTOOL_TRACE_OP", "true")


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
    assert "->" in out

    written = list((root / "traces").glob("*.log"))
    assert len(written) == 1
    content = written[0].read_text(encoding="utf-8")
    for line in lines:
        assert line in content
    assert str(written[0]) in out
    # The receipt's own line count is counted off the written file, not the
    # raw fetched log -- the header line the file also carries counts too.
    m = re.search(r"\[trace\] (\d+) lines", out)
    assert m is not None
    assert int(m.group(1)) == content.count("\n")


def test_trace_does_not_truncate_a_large_log(monkeypatch, tmp_path, capsys) -> None:
    """The whole point: :raw and :grep both cap output; :trace must not."""
    lines = [f"line{i}" for i in range(1, 6001)]  # well past GL_JOB_RAW_MAX_LINES
    rc, root = _run_trace(monkeypatch, tmp_path, ["job.py", "9", "trace"],
                          {"9": {"trace": lines, "status": "failed"}})
    assert rc == 0
    content = (root / "traces").glob("*.log")
    text = next(iter(content)).read_text(encoding="utf-8")
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
    content = written[0].read_text(encoding="utf-8")
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
    assert "numeric" in out
    # Reviewer finding (self-review, #2146): `refuse_job_ids` is now called
    # with op="gl-job-trace", whose real syntax carries no trailing `:trace`
    # (that literal is baked into gl-job-trace's own cmd template). The
    # usage hint must match the op it names, not a stale `gl-job:...:trace`
    # form core itself would refuse for an unconsumed trailing token. Must
    # match the WHOLE line -- "Usage: gl-job-trace:ID1[,ID2,...]" alone is
    # also a substring of the stale "...:trace" form and would not catch it.
    usage_lines = [ln for ln in out.splitlines() if ln.startswith("Usage:")]
    assert usage_lines == ["Usage: gl-job-trace:ID1[,ID2,...]"], out


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
    content = existing.read_text(encoding="utf-8")
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
    content = written[0].read_text(encoding="utf-8")
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


# ---------------------------------------------------------------------------
# review findings (#626 self-review) — filename length, control-char safety,
# line-count accuracy, and a root distinct from gl-issue's attachment dir
# ---------------------------------------------------------------------------

def test_trace_many_ids_does_not_produce_an_unwritable_filename(monkeypatch, tmp_path, capsys) -> None:
    """40 failed jobs in one pipeline is realistic; the filename must not blow
    a filesystem's name-length limit after every trace was already fetched.

    #2105 caps how many ids one call actually fetches (`MAX_TRACE_IDS`), so
    a fetch of 60 is no longer this op's own behaviour — raised here to keep
    this test's own point (the filename, not the fetch count) intact rather
    than folding two different claims into one number.
    """
    monkeypatch.setattr(job, "MAX_TRACE_IDS", 60)
    ids = [str(6900000 + i) for i in range(60)]
    jobs = {i: {"trace": [f"line-{i}"], "status": "failed"} for i in ids}
    argv = ["job.py", ",".join(ids), "trace"]
    rc, root = _run_trace(monkeypatch, tmp_path, argv, jobs)
    assert rc == 0
    written = list((root / "traces").glob("*.log"))
    assert len(written) == 1
    assert len(written[0].name) < 200
    content = written[0].read_text(encoding="utf-8")
    for i in ids:
        assert f"line-{i}" in content


def test_trace_first_failure_is_immune_to_a_stray_control_byte(monkeypatch, tmp_path, capsys) -> None:
    """A vertical tab mid-line must not be read as a line boundary — the same
    class of defect #1119 fixed for every other view in this file."""
    lines = [
        "noise before\x0b1) Foo::bar",
        "Failed asserting that false is true.",
    ]
    rc, root = _run_trace(monkeypatch, tmp_path, ["job.py", "123", "trace"],
                          {"123": {"trace": lines, "status": "failed"}})
    out = capsys.readouterr().out
    assert rc == 0
    # The vertical tab must not be read as a line start — "1) Foo::bar" is
    # never the beginning of a real line in this trace.
    assert "[first] 1) Foo::bar" not in out


def test_trace_line_count_matches_the_written_file(monkeypatch, tmp_path, capsys) -> None:
    jobs = {
        "111": {"trace": ["a1", "a2"], "status": "failed"},
        "222": {"trace": ["b1", "b2"], "status": "failed"},
    }
    rc, root = _run_trace(monkeypatch, tmp_path, ["job.py", "111,222", "trace"], jobs)
    out = capsys.readouterr().out
    assert rc == 0
    written = list((root / "traces").glob("*.log"))
    actual_lines = written[0].read_text(encoding="utf-8").count("\n")
    m = re.search(r"\[trace\] (\d+) lines", out)
    assert m is not None
    assert int(m.group(1)) == actual_lines


def test_trace_root_is_distinct_from_the_issue_attachment_root(monkeypatch, tmp_path, capsys) -> None:
    """`gl-issue` downloads attachments to `_image_root.default_root()` with no
    suffix; the trace writer must not nest inside that same directory."""
    calls: list[str] = []

    def spy(suffix: str = "") -> str:
        calls.append(suffix)
        return str(tmp_path / f"root{suffix}")

    monkeypatch.setattr(job._image_root, "default_root", spy)
    monkeypatch.setattr(sys, "argv", ["job.py", "123", "trace"])
    monkeypatch.setattr(job.subprocess, "run",
                         _make_fake_multi_run({"123": {"trace": ["x"], "status": "failed"}}))
    rc = job.main()
    assert rc == 0
    assert calls == ["-traces"], (
        "the trace root must ask for a suffix distinct from gl-issue's "
        "unsuffixed attachment root, not share it"
    )
