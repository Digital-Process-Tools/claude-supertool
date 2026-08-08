"""#1095 + #1097 — `gl-job:N:fail` claims more than its selector can support.

Two defects, one function, both of the same class this repo keeps filing: an
absence produced by the tool read as an absence in the world.

#1095 — the twin has a branch this one does not
-----------------------------------------------
`gh-job` degrades its header when the job's *status* means error-block
selection cannot have reached the cause (#916, `presets/github/job.py:407`):

    ## Error blocks (11 lines matched) — but see below

`gl-job` prints the unconditional form on every job, cancelled ones included:

    ## All error blocks (6 lines matched, no tail truncation)

"All" and "no tail truncation" are claims about the *selector*, and on a job
that was killed before it produced the failure they are both false about the
*log*.

#1097 — a matched block of pure boilerplate is not a classification
-------------------------------------------------------------------
Filed live on 2026-08-08 against job 7125000. Two halves:

1. The default `error_patterns` contain the bare substring `ERROR`, so
   `ERROR: Job failed: exit code 1` — a line GitLab writes on *every* failed
   job and which is never the cause — is enough on its own to produce
   `## All error blocks (6 lines matched)`. That is the documented job 7021139
   hit: six lines, all teardown, the real Playwright cause never shown.
   Widening the pattern set cannot fix that; *discounting* the boilerplate can.

2. Where a project narrows patterns per job name (`SUPERTOOL_JOB_PATTERNS`),
   the built-in cause markers are the floor the narrowing cannot remove — the
   docstring on `_find_error_sections` says so. That floor has two holes:
   a bare `Error: ...` (nothing before it, so `[\\w\\\\]+(?:Exception|Error):`
   cannot match — this is exactly Playwright's shape) and the MySQL client's
   `ERROR 2026 (HY000):`, which was the cause on line 108 of job 7125000.

Not done here, deliberately: no stderr-stream tier keyed on the `01E` prefix
seen on that trace, and `ERROR: Job failed: exit code N` is not added to any
pattern set. See the PR body.

Logs are synthetic — `glab` is unauthenticated in the sandbox — but shaped line
for line after the two real traces named above.

The bar: every test below fails on the code as it stands.
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


gl_job = _load("gitlab/job.py", "gitlab_job_1095")
gh_job = _load("github/job.py", "github_job_1095")

_REAL_RUN = subprocess.run
GL_ID = "7125000"
GH_ID = "92792057296"

# Job 7021139's shape: a green-looking test run, then teardown, then GitLab's
# own terminal line. Nothing here names a cause.
LOG_BOILERPLATE_ONLY = "\n".join([
    "Running with gitlab-runner 17.1.0",
    "section_start:1750000000:step_script",
    "$ npx playwright test",
    "Running 12 tests using 4 workers",
    "  12 passed (48.1s)",
    "section_end:1750000060:step_script",
    "section_start:1750000060:cleanup_file_variables",
    "Cleaning up project directory and file based variables",
    "section_end:1750000061:cleanup_file_variables",
    "ERROR: Job failed: exit code 1",
])

# The same job with the cause present, in Playwright's shape: `Error:` with
# nothing before it.
LOG_BARE_ERROR = "\n".join([
    "Running with gitlab-runner 17.1.0",
    "section_start:1750000000:step_script",
    "$ npx playwright test",
    "Error: JS errors detected: 3 console errors on /login",
    "    at tests/e2e/login.spec.ts:41:5",
    "section_end:1750000060:step_script",
    "Cleaning up project directory and file based variables",
    "ERROR: Job failed: exit code 1",
])

# Job 7125000, verbatim shape including the `01E` / `00O` prefixes the trace
# carries and the ANSI cleanup does not remove.
LOG_MYSQL = "\n".join([
    "Running with gitlab-runner 17.1.0",
    "section_start:1750000000:step_script",
    "$ mysql --host=db --ssl-mode=DISABLED < schema.sql",
    "01E ERROR 2026 (HY000): TLS/SSL error: SSL is required, but the server does not support it",
    "section_end:1750000060:step_script",
    "Cleaning up project directory and file based variables",
    "00O ERROR: Job failed: exit code 1",
])

# A runner-side failure. `ERROR: Job failed (system failure)` IS the cause, and
# must not be swept up with `exit code N` boilerplate.
LOG_SYSTEM_FAILURE = "\n".join([
    "Running with gitlab-runner 17.1.0",
    "section_start:1750000000:prepare_executor",
    "Preparing environment",
    "section_end:1750000002:prepare_executor",
    "ERROR: Job failed (system failure): prepare environment: pod not found",
])

# PHPUnit narrowing, as configured on the DVSI project for this job name.
PHPUNIT_ONLY = "FAILURES!,Failed asserting,Tests:"


def _gl_meta(status: str) -> dict[str, Any]:
    return {
        "id": int(GL_ID),
        "name": "conformity_prepare",
        "status": status,
        "stage": "prepare",
        "duration": 12.0,
        "web_url": f"https://gitlab.example/-/jobs/{GL_ID}",
        "ref": "",
        "pipeline": {"id": 999},
    }


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


def _run_gl(monkeypatch, capsys, status, log, patterns=None) -> tuple[int, str]:
    monkeypatch.setattr(sys, "argv", ["job.py", GL_ID, "fail"])
    monkeypatch.setattr(gl_job.subprocess, "run", _fake_glab(_gl_meta(status), log))
    if patterns is not None:
        monkeypatch.setenv("SUPERTOOL_ERROR_PATTERNS", patterns)
    rc = gl_job.main()
    return rc, capsys.readouterr().out


# ---------------------------------------------------------------------------
# #1097 — boilerplate-only is a refusal, not a block
# ---------------------------------------------------------------------------

def test_a_block_of_pure_boilerplate_is_not_headlined_as_error_blocks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _, out = _run_gl(monkeypatch, capsys, "failed", LOG_BOILERPLATE_ONLY)
    assert "All error blocks" not in out, (
        "every line that matched is a line GitLab writes on every failed job; "
        "headlining them as the error blocks is a confident, useless answer"
    )


def test_boilerplate_only_says_a_pattern_is_missing_rather_than_nothing_is_wrong(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _, out = _run_gl(monkeypatch, capsys, "failed", LOG_BOILERPLATE_ONLY)
    assert "pattern is missing here" in out
    assert "Log tail" in out


def test_boilerplate_only_still_shows_the_lines_it_discounted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Disclosure, not suppression — hiding the matches would be the same
    defect pointed the other way.

    Asserted against the *discounted* section, not against stdout at large:
    the pre-fix code also printed this line, inside the block whose header is
    the bug, so a bare substring check would pass either way.
    """
    _, out = _run_gl(monkeypatch, capsys, "failed", LOG_BOILERPLATE_ONLY)
    assert "Shown, not hidden:" in out
    shown = out.split("Shown, not hidden:", 1)[1].split("## Log tail", 1)[0]
    assert "ERROR: Job failed: exit code 1" in shown


def test_a_system_failure_line_is_a_cause_and_not_boilerplate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """`ERROR: Job failed (system failure): ...` names why the job died. Only
    `exit code N` is the contentless form."""
    _, out = _run_gl(monkeypatch, capsys, "failed", LOG_SYSTEM_FAILURE)
    assert "All error blocks" in out
    assert "pod not found" in out


# ---------------------------------------------------------------------------
# #1097 — the cause-marker floor a per-job pattern table cannot remove
# ---------------------------------------------------------------------------

def test_a_bare_error_prefix_is_a_cause_marker(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _, out = _run_gl(monkeypatch, capsys, "failed", LOG_BARE_ERROR, PHPUNIT_ONLY)
    assert "JS errors detected" in out
    assert "no error pattern matched" not in out


def test_the_mysql_clients_error_line_is_a_cause_marker(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _, out = _run_gl(monkeypatch, capsys, "failed", LOG_MYSQL, PHPUNIT_ONLY)
    assert "TLS/SSL error" in out
    assert "no error pattern matched" not in out


# ---------------------------------------------------------------------------
# #1095 — the selector does not fit every status
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["canceled", "skipped", "manual"])
def test_fail_does_not_claim_all_error_blocks_when_the_job_did_not_fail(
    status: str, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, out = _run_gl(monkeypatch, capsys, status, LOG_MYSQL)
    assert "All error blocks" not in out, (
        f"a `{status}` job's :fail still headlines its selection as complete"
    )


def test_fail_names_the_status_and_a_fallback_op(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _, out = _run_gl(monkeypatch, capsys, "canceled", LOG_MYSQL)
    assert "canceled" in out
    assert "error-block selection" in out.lower()
    assert f"gl-job:{GL_ID}:raw:" in out


def test_a_cancelled_job_with_no_matches_still_points_somewhere(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _, out = _run_gl(monkeypatch, capsys, "canceled", LOG_BOILERPLATE_ONLY,
                     PHPUNIT_ONLY)
    assert "error-block selection" in out.lower()


def test_a_genuine_failure_keeps_the_complete_claim(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """`:fail` is the right selector for a job that failed — do not water it
    down."""
    _, out = _run_gl(monkeypatch, capsys, "failed", LOG_MYSQL)
    assert "All error blocks" in out
    assert "error-block selection" not in out.lower()


# ---------------------------------------------------------------------------
# the twins, pinned together
# ---------------------------------------------------------------------------
# #1095 is the second live instance in one afternoon of gh-job and gl-job
# answering the same question in different shapes, and #1066 already added a
# string-equality parity test for one gap marker. A shared helper was
# considered and rejected: two of the four disclosure lines are forge-specific
# (op name, status vocabulary), so the shared core would be an f-string with
# three parameters — and what actually drifted was a *missing call*, which no
# amount of sharing prevents and this test does catch.

GH_CANCELLED_LOG = "\n".join([
    "....................................... [ 96%]",
    "##[error]The operation was canceled.",
    "Post job cleanup.",
    "Terminate orphan process: pid (2505) (python)",
])


def _fake_gh(log: str):
    meta = {
        "id": int(GH_ID),
        "name": "coverage",
        "status": "completed",
        "conclusion": "cancelled",
        "run_id": 31153214310,
        "html_url": f"https://github.com/o/r/actions/runs/1/job/{GH_ID}",
        "completed_at": "2026-08-06T10:00:00Z",
    }

    def fake_run(args: list[str], **kw: Any) -> subprocess.CompletedProcess:
        if args and args[0] == "git":
            return _REAL_RUN(args, **kw)
        assert args and args[0] == "gh", f"unstubbed command: {args!r}"
        cmd = args[1] if len(args) > 1 else ""
        url = args[2] if len(args) > 2 else ""
        if cmd == "api" and url.split("?")[0].endswith("/logs"):
            return subprocess.CompletedProcess(args, 0, log, "")
        if cmd == "api" and "/actions/jobs/" in url:
            return subprocess.CompletedProcess(args, 0, json.dumps(meta), "")
        if cmd == "run" and url == "view":
            return subprocess.CompletedProcess(
                args, 0,
                json.dumps({"headBranch": "b", "event": "push",
                            "pullRequests": []}), "")
        raise AssertionError(f"unstubbed gh call: {args!r}")

    return fake_run


def test_both_twins_disclose_when_the_selector_does_not_fit_the_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["job.py", GH_ID, "fail"])
    monkeypatch.setattr(gh_job.subprocess, "run", _fake_gh(GH_CANCELLED_LOG))
    gh_job.main()
    gh_out = capsys.readouterr().out

    _, gl_out = _run_gl(monkeypatch, capsys, "canceled", LOG_MYSQL)

    for op, out in (("gh-job", gh_out), ("gl-job", gl_out)):
        assert "All error blocks" not in out, f"{op} still overclaims"
        assert "error-block selection" in out.lower(), f"{op} does not disclose"
