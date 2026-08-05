"""#827 — `gh-job:ID` answers for a check run too, under the check's own header.

#793/#821 taught `gh-job` to *recognise* a check-run id and then decline to read
it, on the grounds that answering would be "a probe that silently changes which
API answered". Florian's objection, which is this issue: a user should not have
to know GitHub keeps CI results in two id namespaces and pick a different op
depending on which one a leg landed in. GitLab needs no equivalent — one
pipeline, one hierarchy, one id space — so the seam is GitHub's history leaking
into our command surface. The reasoning in #821 is right about *silently* and
wrong about *answering*: a render whose header names the namespace it read is
not silent, it is labelled.

What is verified live, and what is fixture
------------------------------------------
**Verified live on 2026-08-05** against `Digital-Process-Tools/claude-supertool`
with an authenticated `gh`:

  * An Actions job's `id` and its check run's id are **the same integer**, and
    GitHub says so itself: `actions/runs/30999833522/jobs` returns
    `id: 92285746490` together with
    `check_run_url: .../check-runs/92285746490`. The two namespaces are not two
    things sharing an id — for an Actions leg they are one leg in two
    projections.
  * An App-authored check run 404s in the Actions namespace:
    `actions/jobs/92264897684` → `HTTP 404`, while `check-runs/92264897684`
    returns `CodeQL` / `github-advanced-security`.

Which is why **the bare integer is not actually ambiguous**, given an order:
ask Actions first, and an answer there is definitive (the check run with that
id is the same leg). Only a 404 from Actions sends the question to the checks
API. The residual uncertainty is not "which of two things is it" but "one of
the two routes did not answer" — and that is the third state, which declines.
`test_..._declines_when_the_checks_probe_does_not_answer` is the guard on it.

Still fixture-only: every response body below, all 404/502 shapes, and the
annotations page.

Stubbing note (#731): the fake gh dispatches per endpoint and raises on any
call it does not recognise. That is what enforces both "no extra request on any
working path" and "the routed render fetches the check-run object once".
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

PRESETS = Path(__file__).parent.parent / "presets"


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, PRESETS / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


job = _load("github/job.py", "github_job_827")

_REAL_RUN = subprocess.run

NO_SUCH_STDERR = "gh: Not Found (HTTP 404)\n"
BLOB_NOT_FOUND_STDOUT = (
    '﻿<?xml version="1.0" encoding="utf-8"?><Error><Code>BlobNotFound</Code>'
    "<Message>The specified blob does not exist.</Message></Error>"
)

CHECK_ID = "92264897684"


def _check_run(**over: Any) -> dict[str, Any]:
    base = {
        "id": int(CHECK_ID),
        "name": "CodeQL",
        "status": "completed",
        "conclusion": "failure",
        "head_sha": "f00dcafe" * 5,
        "html_url": f"https://github.com/o/r/runs/{CHECK_ID}",
        "app": {"slug": "github-advanced-security"},
        "output": {
            "title": "1 new alert including 1 high severity security vulnerability",
            "summary": "**1 new alert** including 1 high severity security vulnerability",
            "annotations_count": 1,
        },
    }
    base.update(over)
    return base


def _annotation(**over: Any) -> dict[str, Any]:
    base = {
        "path": "presets/github/issue.py",
        "start_line": 125,
        "end_line": 125,
        "annotation_level": "failure",
        "title": "Incomplete URL substring sanitization",
        "message": (
            "The string github.com may be at an arbitrary position in the "
            "sanitized URL."
        ),
        "raw_details": None,
    }
    base.update(over)
    return base


def _fake_gh(
    *,
    job_meta: dict[str, Any] | None = None,
    job_rc: int = 1,
    job_stderr: str = NO_SUCH_STDERR,
    check: dict[str, Any] | None = None,
    check_rc: int | None = None,
    check_stderr: str = NO_SUCH_STDERR,
    annotations: list[dict[str, Any]] | None = None,
    ann_rc: int = 0,
    ann_stderr: str = "",
    log_rc: int = 1,
    log_stdout: str = BLOB_NOT_FOUND_STDOUT,
    log_stderr: str = NO_SUCH_STDERR,
    seen: list[str] | None = None,
):
    """One branch per gh endpoint. Anything else is an AssertionError, not a stub."""
    if check_rc is None:
        check_rc = 0 if check is not None else 1

    def fake_run(args: list[str], **kw: Any) -> subprocess.CompletedProcess:
        if args and args[0] == "git":
            return _REAL_RUN(args, **kw)
        assert args and args[0] == "gh", f"unstubbed command: {args!r}"
        cmd = args[1] if len(args) > 1 else ""
        url = args[2] if len(args) > 2 else ""
        if seen is not None:
            seen.append(f"{cmd} {url}")
        if cmd == "api" and "/check-runs/" in url and url.split("?")[0].endswith("/annotations"):
            return subprocess.CompletedProcess(
                args, ann_rc, json.dumps(annotations or []), ann_stderr)
        if cmd == "api" and "/check-runs/" in url:
            return subprocess.CompletedProcess(
                args, check_rc, json.dumps(check) if check is not None else "",
                check_stderr)
        if cmd == "api" and url.split("?")[0].endswith("/logs"):
            return subprocess.CompletedProcess(args, log_rc, log_stdout, log_stderr)
        if cmd == "api" and "/actions/jobs/" in url:
            return subprocess.CompletedProcess(
                args, job_rc, json.dumps(job_meta) if job_meta is not None else "",
                job_stderr)
        if cmd == "run" and url == "view":
            return subprocess.CompletedProcess(
                args, 0,
                json.dumps({"headBranch": "b", "event": "push", "pullRequests": []}), "")
        raise AssertionError(f"unstubbed gh call: {args!r}")

    return fake_run


def _run(monkeypatch, capsys, fake, argv) -> tuple[int, str]:
    monkeypatch.setattr(sys, "argv", list(argv))
    monkeypatch.setattr(job.subprocess, "run", fake)
    rc = job.main()
    return rc, capsys.readouterr().out


# ==========================================================================
# The routing itself
# ==========================================================================

def test_gh_job_renders_the_check_run_it_was_handed(monkeypatch, capsys) -> None:
    """The issue, stated as a test: the caller does not have to pick the op.

    Fails on the shipped 0.23 behaviour, which prints an ERROR naming
    `gh-check` and exits 1 having rendered nothing.
    """
    rc, out = _run(monkeypatch, capsys,
                   _fake_gh(check=_check_run(), annotations=[_annotation()]),
                   ("job.py", CHECK_ID, "fail"))
    assert rc == 0
    assert "presets/github/issue.py:125" in out
    assert "Incomplete URL substring sanitization" in out
    assert "arbitrary position in the sanitized URL" in out


def test_the_routed_render_never_wears_a_job_header(monkeypatch, capsys) -> None:
    """#821's invariant survives the routing — it is the reason routing is safe.

    Answering is fine; answering *as though a job had answered* is not. A job
    renders as a log; a check run renders as status + output + annotations.
    """
    _, out = _run(monkeypatch, capsys,
                  _fake_gh(check=_check_run(), annotations=[_annotation()]),
                  ("job.py", CHECK_ID, "fail"))
    assert "# Job #" not in out
    assert f"# Check run #{CHECK_ID}" in out


def test_the_routed_render_names_the_namespace_that_answered(
        monkeypatch, capsys) -> None:
    """Labelled, not silent. The disclosure is the whole permission to route."""
    _, out = _run(monkeypatch, capsys,
                  _fake_gh(check=_check_run(), annotations=[_annotation()]),
                  ("job.py", CHECK_ID, "fail"))
    assert "checks API" in out
    # And it says the routing happened, rather than leaving the reader to
    # wonder why `gh-job` printed a check run.
    assert "gh-job" in out
    assert "not an Actions job" in out


def test_the_routed_render_says_the_log_mode_does_not_apply(
        monkeypatch, capsys) -> None:
    """`:fail` asks for blocks of a log. A check run has no log — say so.

    Silently dropping a requested mode is the same class of quiet as rendering
    an absence: the reader asked a question that was never answered and is not
    told which one.
    """
    _, out = _run(monkeypatch, capsys,
                  _fake_gh(check=_check_run(), annotations=[_annotation()]),
                  ("job.py", CHECK_ID, "fail"))
    low = out.lower()
    assert "fail" in low and "no log" in low


def test_raw_mode_is_declined_by_name_too(monkeypatch, capsys) -> None:
    _, out = _run(monkeypatch, capsys,
                  _fake_gh(check=_check_run(), annotations=[_annotation()]),
                  ("job.py", CHECK_ID, "raw"))
    assert "no log" in out.lower()
    assert "raw" in out.lower()


# ==========================================================================
# The three states — routing must not create a fourth, quieter one
# ==========================================================================

def test_both_namespaces_404_still_blames_the_id(monkeypatch, capsys) -> None:
    rc, out = _run(monkeypatch, capsys, _fake_gh(check=None),
                   ("job.py", "99999999999", "fail"))
    assert rc == 1
    assert "Check the ID" in out
    assert "does exist" not in out


def test_declines_when_the_checks_probe_does_not_answer(
        monkeypatch, capsys) -> None:
    """The residual uncertainty, and the only one that survives the ordering.

    A 502 on the checks call is not evidence there is no check run. The op must
    not render an empty check, and must not fall back to "no such job".
    """
    rc, out = _run(monkeypatch, capsys,
                   _fake_gh(check=None, check_rc=1,
                            check_stderr="gh: HTTP 502 Bad Gateway\n"),
                   ("job.py", CHECK_ID, "fail"))
    assert rc == 1
    low = out.lower()
    assert "no such job exists in this repo" not in out
    assert "unknown" in low or "could not" in low
    assert "502" in out
    assert "# Check run #" not in out
    assert "Annotations (0)" not in out


def test_routed_zero_annotations_is_still_not_an_all_clear(
        monkeypatch, capsys) -> None:
    """The routed path inherits the check renderer's refusal to say nothing-found."""
    rc, out = _run(monkeypatch, capsys,
                   _fake_gh(check=_check_run(), annotations=[]),
                   ("job.py", CHECK_ID, "fail"))
    assert rc == 0
    assert "not an all-clear" in out.lower()


def test_routed_unreadable_annotations_are_not_reported_as_zero(
        monkeypatch, capsys) -> None:
    """"The fetch failed" and "there are none" stay two different sentences."""
    rc, out = _run(monkeypatch, capsys,
                   _fake_gh(check=_check_run(), ann_rc=1,
                            ann_stderr="gh: HTTP 500\n"),
                   ("job.py", CHECK_ID, "fail"))
    assert rc == 1
    assert "UNKNOWN" in out
    assert "Annotations (0)" not in out


# ==========================================================================
# Cost — routing is free on every path that already worked
# ==========================================================================

def test_happy_path_makes_no_check_runs_call(monkeypatch, capsys) -> None:
    """An Actions job that answers is definitive: never ask the other namespace."""
    seen: list[str] = []
    fake = _fake_gh(
        job_meta={"name": "pytest", "status": "completed", "conclusion": "failure",
                  "run_id": 1, "completed_at": "2026-08-01T00:00:00Z"},
        job_rc=0, log_rc=0, log_stdout="hello\nERROR: boom\n", log_stderr="",
        seen=seen)
    rc, _ = _run(monkeypatch, capsys, fake, ("job.py", "123", "fail"))
    assert rc == 0
    assert not any("/check-runs" in s for s in seen), seen


def test_a_job_that_answered_is_never_re_asked_as_a_check_run(
        monkeypatch, capsys) -> None:
    """A missing log on a job that *exists* is #723's question, not #827's.

    A queued or in-progress job answers at the job endpoint and 404s at the log
    endpoint. Its id also resolves as a check run — GitHub mints one per
    Actions job sharing the integer — so a router keyed on the log 404 alone
    would render the check-run projection of a running job in place of "the log
    is not written yet". Routing is keyed on the *job endpoint* 404 for that
    reason, and this is the guard on it.
    """
    seen: list[str] = []
    fake = _fake_gh(
        job_meta={"name": "pytest", "status": "in_progress", "conclusion": None,
                  "run_id": 1, "completed_at": None},
        job_rc=0, seen=seen)
    rc, out = _run(monkeypatch, capsys, fake, ("job.py", "123", "fail"))
    assert rc == 1
    assert "in_progress" in out
    assert "# Check run #" not in out
    assert not any("/check-runs" in s for s in seen), seen


def test_the_unrouted_fallthrough_asks_the_checks_api_once(
        monkeypatch, capsys) -> None:
    """The message path threads the probe it already made, rather than re-asking.

    `_missing_log_message` can still probe on its own — it is reachable from
    elsewhere — so a version that ignores the threaded result is green on every
    output assertion and doubles a request on the both-404 path.
    """
    seen: list[str] = []
    rc, _ = _run(monkeypatch, capsys, _fake_gh(check=None, seen=seen),
                 ("job.py", "99999999999", "fail"))
    assert rc == 1
    assert len([s for s in seen if "/check-runs/" in s]) == 1, seen


def test_the_routed_render_reads_the_check_run_object_once(
        monkeypatch, capsys) -> None:
    """Probe-then-render must not fetch the same object twice.

    A half-implementation that leaves `_probe_check_run` in place and calls the
    whole of `gh-check` afterwards passes every assertion above and doubles the
    request count on this path. This is the one that catches it.
    """
    seen: list[str] = []
    rc, _ = _run(monkeypatch, capsys,
                 _fake_gh(check=_check_run(), annotations=[_annotation()],
                          seen=seen),
                 ("job.py", CHECK_ID, "fail"))
    assert rc == 0
    objects = [s for s in seen
               if "/check-runs/" in s and not s.split("?")[0].endswith("/annotations")]
    assert len(objects) == 1, seen
