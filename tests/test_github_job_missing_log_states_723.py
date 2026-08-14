"""gh-job must say *why* a log is missing, not blame the ID (#723).

GitHub writes a job's log on completion. So a 404 from
`repos/.../actions/jobs/<id>/logs` has at least four causes that mean
different things to the caller, and the op used to render all of them as
"Check the ID" — the one thing that was demonstrably right in the incident
that filed the issue.

Verified live against this repo on 2026-08-01:

    job 91386522337 (status: queued)      logs -> rc 1, stderr "gh: HTTP 404",
                                          stdout "<Error><Code>BlobNotFound</Code>"
    job 91386522329 (status: in_progress) logs -> identical
    job 99999999999 (does not exist)      logs -> rc 1, stderr
                                          "gh: Not Found (HTTP 404)"
                                          and the *job* endpoint 404s too

The job endpoint is what separates them, and `job.py` already calls it
before it fetches the log — so this costs no extra request on any path.

Stubbing note (#731): the fake below dispatches per gh endpoint and raises
on any call it does not recognise. A wholesale `subprocess.run` double
silently absorbs every new call an implementation makes, which is how a
test keeps passing while the code underneath it changes shape.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

PRESET_PATH = Path(__file__).parent.parent / "presets" / "github" / "job.py"
_spec = importlib.util.spec_from_file_location("github_job_723", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
job = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(job)

_REAL_RUN = subprocess.run

# What `gh repo view --json nameWithOwner` answers for this checkout. The op
# reads it on the paths that print a `gh api …` command for a reader to paste,
# so those name a repository rather than gh's cwd placeholders (#1679).
CWD_SLUG = "Digital-Process-Tools/claude-supertool"

# The exact bytes gh emits for a job that exists but whose log blob does not.
BLOB_NOT_FOUND_STDOUT = (
    '﻿<?xml version="1.0" encoding="utf-8"?><Error><Code>BlobNotFound</Code>'
    "<Message>The specified blob does not exist.\n"
    "RequestId:85e2e34f-b01e-00ea-6fcc-21b0df000000\n"
    "Time:2026-08-01T15:44:19.7419341Z</Message></Error>"
)
BLOB_NOT_FOUND_STDERR = "gh: HTTP 404\n"
NO_SUCH_JOB_STDERR = "gh: Not Found (HTTP 404)\n"


def _dispatching_run(
    *,
    meta: dict[str, Any] | None = None,
    meta_rc: int = 0,
    meta_stderr: str = "",
    log_rc: int = 1,
    log_stdout: str = BLOB_NOT_FOUND_STDOUT,
    log_stderr: str = BLOB_NOT_FOUND_STDERR,
):
    """Fake gh, one branch per endpoint. Unknown calls are loud, never absorbed."""
    meta_json = json.dumps(meta) if meta is not None else ""

    def fake_run(args: list[str], **kw: Any) -> subprocess.CompletedProcess:
        if args and args[0] == "git":
            return _REAL_RUN(args, **kw)
        assert args and args[0] == "gh", f"unstubbed command: {args!r}"
        cmd = args[1] if len(args) > 1 else ""
        url = args[2] if len(args) > 2 else ""
        if cmd == "api" and url.endswith("/logs"):
            return subprocess.CompletedProcess(args, log_rc, log_stdout, log_stderr)
        if cmd == "api" and "/check-runs/" in url:
            # #793: when both the job endpoint and the log 404, the op asks the
            # checks API whether the id is a check run before it says the id
            # names nothing. Absent here — this suite is about Actions jobs.
            return subprocess.CompletedProcess(args, 1, "", NO_SUCH_JOB_STDERR)
        if cmd == "api" and "/actions/jobs/" in url:
            return subprocess.CompletedProcess(args, meta_rc, meta_json, meta_stderr)
        if cmd == "run" and url == "view":
            return subprocess.CompletedProcess(
                args, 0,
                json.dumps({"headBranch": "some-branch", "event": "push",
                            "pullRequests": []}),
                "",
            )
        if cmd == "repo" and url == "view":
            # #1679: both blocks below print a `gh api …` command for the
            # reader to paste, and a pasted `repos/{owner}/{repo}/…` names
            # whatever repository the paster is standing in. The slug is read
            # back so the printed line names one.
            return subprocess.CompletedProcess(
                args, 0, json.dumps({"nameWithOwner": CWD_SLUG}), "")
        raise AssertionError(f"unstubbed gh call: {args!r}")

    return fake_run


def _job_meta(**over: Any) -> dict[str, Any]:
    base = {
        "name": "pytest (ubuntu-latest, 3.11)",
        "status": "completed",
        "conclusion": "failure",
        "run_id": 30706535109,
        "run_url": "https://github.com/x/y/actions/runs/30706535109",
        "completed_at": "2026-08-01T15:44:19Z",
    }
    base.update(over)
    return base


def _run(monkeypatch, capsys, fake, argv=("job.py", "91386522337")) -> tuple[int, str]:
    monkeypatch.setattr(sys, "argv", list(argv))
    monkeypatch.setattr(job.subprocess, "run", fake)
    rc = job.main()
    return rc, capsys.readouterr().out


# --- 1. cancelled: the row that saves real time -----------------------------

def test_cancelled_job_says_no_log_will_ever_exist(monkeypatch, capsys) -> None:
    rc, out = _run(monkeypatch, capsys, _dispatching_run(
        meta=_job_meta(status="completed", conclusion="cancelled")))
    assert rc == 1
    assert "cancelled" in out
    assert "never" in out.lower() or "no log was ever written" in out.lower()
    assert "Check the ID" not in out


# --- 2/3. not finished yet: come back later, do not blame the ID ------------

def test_in_progress_job_says_log_is_not_written_yet(monkeypatch, capsys) -> None:
    rc, out = _run(monkeypatch, capsys, _dispatching_run(
        meta=_job_meta(status="in_progress", conclusion=None, completed_at=None)))
    assert rc == 1
    assert "in_progress" in out
    assert "not written yet" in out or "not been written" in out
    assert "Check the ID" not in out
    assert "cancelled" not in out


def test_queued_job_names_its_status_not_the_id(monkeypatch, capsys) -> None:
    rc, out = _run(monkeypatch, capsys, _dispatching_run(
        meta=_job_meta(status="queued", conclusion=None, completed_at=None)))
    assert rc == 1
    assert "queued" in out
    assert "Check the ID" not in out


def test_skipped_job_is_in_the_stop_looking_family(monkeypatch, capsys) -> None:
    rc, out = _run(monkeypatch, capsys, _dispatching_run(
        meta=_job_meta(status="completed", conclusion="skipped")))
    assert rc == 1
    assert "skipped" in out
    assert "never" in out.lower() or "no log was ever written" in out.lower()
    assert "Check the ID" not in out


# --- 4. the ID really is wrong: keep the original message -------------------

def test_nonexistent_job_still_tells_you_to_check_the_id(monkeypatch, capsys) -> None:
    rc, out = _run(monkeypatch, capsys, _dispatching_run(
        meta=None, meta_rc=1, meta_stderr=NO_SUCH_JOB_STDERR,
        log_stdout='{"message":"Not Found","status":"404"}',
        log_stderr=NO_SUCH_JOB_STDERR))
    assert rc == 1
    assert "Check the ID" in out
    assert "cancelled" not in out
    assert "not written yet" not in out


# --- 5. completed, log gone: expired or purged, named as such ---------------

def test_completed_job_with_missing_log_names_conclusion_and_time(
    monkeypatch, capsys
) -> None:
    rc, out = _run(monkeypatch, capsys, _dispatching_run(
        meta=_job_meta(status="completed", conclusion="failure",
                       completed_at="2026-02-11T09:03:00Z")))
    assert rc == 1
    assert "failure" in out
    assert "2026-02-11T09:03:00Z" in out
    assert "expired" in out.lower() or "purged" in out.lower()
    assert "Check the ID" not in out


# --- 6. the third state: supertool could not tell, and says so --------------

def test_unreadable_metadata_declines_instead_of_guessing(
    monkeypatch, capsys
) -> None:
    """Job endpoint failed for a reason that is not 404 — state is unknowable."""
    rc, out = _run(monkeypatch, capsys, _dispatching_run(
        meta=None, meta_rc=1,
        meta_stderr="gh: API rate limit exceeded (HTTP 429)\n"))
    assert rc == 1
    # Must not pick any of the four answers it cannot distinguish.
    assert "Check the ID" not in out
    assert "cancelled" not in out
    assert "not written yet" not in out
    # Must name the check that declined.
    assert "could not" in out.lower()
    assert "429" in out or "rate limit" in out.lower()


# --- 7. non-404 log errors keep their own classification --------------------

def test_unauthenticated_log_fetch_keeps_its_own_message(
    monkeypatch, capsys
) -> None:
    rc, out = _run(monkeypatch, capsys, _dispatching_run(
        meta=_job_meta(), log_rc=1, log_stdout="",
        log_stderr="gh: Requires authentication (HTTP 401)\n"))
    assert rc == 1
    assert "gh auth login" in out
    assert "cancelled" not in out


# --- 8. the loud failure stays loud ----------------------------------------

def test_missing_log_never_renders_as_an_ok_or_an_empty_log(
    monkeypatch, capsys
) -> None:
    for meta in (
        _job_meta(status="completed", conclusion="cancelled"),
        _job_meta(status="in_progress", conclusion=None, completed_at=None),
        _job_meta(status="completed", conclusion="success"),
    ):
        rc, out = _run(monkeypatch, capsys, _dispatching_run(meta=meta),
                       argv=("job.py", "91386522337", "raw"))
        assert rc == 1, f"missing log returned success for {meta['status']}"
        assert out.startswith("ERROR:") or "\nERROR:" in out
        assert "Raw lines" not in out
        assert "Log: 0 lines total" not in out


# --- 9. empty is not absent -------------------------------------------------

def test_an_empty_log_is_reported_as_empty_not_as_absent(
    monkeypatch, capsys
) -> None:
    """`gh run view --log` returning nothing for a real failure is the other
    lie this surface tells (#723). A 0-line log fetched successfully must
    read as empty, and must not borrow the vocabulary of a missing one."""
    rc, out = _run(monkeypatch, capsys, _dispatching_run(
        meta=_job_meta(conclusion="failure"), log_rc=0, log_stdout="",
        log_stderr=""))
    assert rc == 0
    assert "empty" in out.lower()
    assert "not found" not in out.lower()
    assert "cancelled" not in out
    # #1679: the cross-check command is pasted by a reader, so it names a
    # repository rather than gh's cwd-resolved placeholders.
    assert f"gh api repos/{CWD_SLUG}/actions/jobs/" in out, out
    assert "repos/{owner}/{repo}/" not in out, out
