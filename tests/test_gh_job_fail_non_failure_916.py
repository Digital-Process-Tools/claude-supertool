"""#916 — `:fail` on a job that did not fail claims completeness it does not have.

Reproduced live on 2026-08-07 against the job named in the issue:

    $ python3 supertool.py 'gh-job:92792057296:fail'
    # Job #92792057296 — coverage (floors + disclosure)
    Status: cancelled
    Log: 371 lines total

    ## All error blocks (11 lines matched, no tail truncation)
    ...
        331 | ....................................................... [ 96%]
        332 | ##[error]The operation was canceled.
        333 | Post job cleanup.

Still exactly as filed. `## All error blocks (11 lines matched, no tail
truncation)` is a claim of completeness: the reader is told the selector found
everything there was and that nothing was cut. Both are true *of the selector*
and neither is true of the log — the `Terminate orphan process` block thirty
lines later carries no error marker, so no error pattern can reach it.

The op knows this and prints it. `Status: cancelled` is on line 2 of the same
render, produced from the same `display_status` the selection branch reads. So
the op holds, at selection time, the fact that "error blocks" is the wrong
selector for this job, and applies it anyway.

Disclosure, not pattern-widening
--------------------------------
Widening the pattern set to catch `Terminate orphan process` would fix this log
and no other, and it trades a loud wrong answer for a quiet one: the next
cancelled job's tell is some other unmarked line, and a *wider* set that still
misses it produces a longer, more confident-looking block. A pattern set cannot
be complete, so the honest output is "these N lines matched, and on a
`cancelled` job that selector is not where the cause lives — here is what to
run instead".

Deliberately NOT fixed here: the sibling `gl-job` gap (Playwright's
`Error: JS errors detected:`), where the conclusion *is* `failure` and there is
no signal to key a disclosure on. Same symptom, no trigger available; keying
this disclosure on the conclusion would not touch it and pretending otherwise
would be the same overclaim one repo over.

The bar: each test below fails on the code as it stands.
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


job = _load("github/job.py", "github_job_916")

_REAL_RUN = subprocess.run
JOB_ID = "92792057296"

# Shaped after the real trace: progress lines, the single cancellation error,
# then the teardown block that holds the actual diagnostic and carries no
# error marker of any kind.
LOG_WITH_ERROR_LINE = "\n".join(
    [f"....................................... [ {n}%]" for n in range(90, 97)]
    + [
        "##[error]The operation was canceled.",
        "Post job cleanup.",
        "Cleaning up orphan processes",
        "Terminate orphan process: pid (2505) (python)",
        "Terminate orphan process: pid (2506) (python)",
        "Terminate orphan process: pid (2507) (python)",
    ]
)

# A cancelled job whose log holds no error marker at all — the other half of
# the same defect: `## No error patterns matched` and no route onward.
LOG_WITHOUT_ERROR_LINE = "\n".join(
    [f"....................................... [ {n}%]" for n in range(90, 97)]
    + [
        "Cleaning up orphan processes",
        "Terminate orphan process: pid (2505) (python)",
    ]
)


def _meta(conclusion: str) -> dict[str, Any]:
    return {
        "id": int(JOB_ID),
        "name": "coverage (floors + disclosure)",
        "status": "completed",
        "conclusion": conclusion,
        "run_id": 31153214310,
        "html_url": f"https://github.com/o/r/actions/runs/31153214310/job/{JOB_ID}",
        "completed_at": "2026-08-06T10:00:00Z",
    }


def _fake_gh(meta: dict[str, Any], log: str):
    def fake_run(args: list[str], **kw: Any) -> subprocess.CompletedProcess:
        if args and args[0] == "git":
            return _REAL_RUN(args, **kw)
        assert args and args[0] == "gh", f"unstubbed command: {args!r}"
        cmd = args[1] if len(args) > 1 else ""
        # First non-flag positional after `api` — the log call now inserts
        # --allow-escape-sequences before the url (#1957).
        url = next((a for a in args[2:] if not a.startswith("--")), "")
        if cmd == "api" and url.split("?")[0].endswith("/logs"):
            return subprocess.CompletedProcess(args, 0, log, "")
        if cmd == "api" and "/actions/jobs/" in url:
            return subprocess.CompletedProcess(args, 0, json.dumps(meta), "")
        if cmd == "run" and url == "view":
            return subprocess.CompletedProcess(
                args, 0,
                json.dumps({"headBranch": "b", "event": "push", "pullRequests": []}), "")
        raise AssertionError(f"unstubbed gh call: {args!r}")

    return fake_run


def _run(monkeypatch, capsys, meta, log, argv) -> tuple[int, str]:
    monkeypatch.setattr(sys, "argv", list(argv))
    monkeypatch.setattr(job.subprocess, "run", _fake_gh(meta, log))
    rc = job.main()
    return rc, capsys.readouterr().out


# ---------------------------------------------------------------------------
# the claim of completeness
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("conclusion", ["cancelled", "timed_out", "skipped"])
def test_fail_does_not_claim_all_error_blocks_when_the_job_did_not_fail(
    conclusion: str, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc, out = _run(monkeypatch, capsys, _meta(conclusion), LOG_WITH_ERROR_LINE,
                   ["job.py", JOB_ID, "fail"])
    assert rc == 0
    assert "All error blocks" not in out, (
        f"a `{conclusion}` job's :fail still headlines its selection as complete"
    )


@pytest.mark.parametrize("conclusion", ["cancelled", "timed_out"])
def test_fail_says_the_selector_is_a_poor_fit_and_names_the_conclusion(
    conclusion: str, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, out = _run(monkeypatch, capsys, _meta(conclusion), LOG_WITH_ERROR_LINE,
                  ["job.py", JOB_ID, "fail"])
    assert conclusion in out
    assert "error-block selection" in out.lower()


@pytest.mark.parametrize("log", [LOG_WITH_ERROR_LINE, LOG_WITHOUT_ERROR_LINE])
def test_fail_names_a_fallback_op_the_reader_can_actually_run(
    log: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """An error that says what is wrong but not what to do is its own filing."""
    _, out = _run(monkeypatch, capsys, _meta("cancelled"), log,
                  ["job.py", JOB_ID, "fail"])
    assert f"gh-job:{JOB_ID}:raw:" in out


def test_the_matched_lines_are_still_shown(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Disclosure, not suppression — trading a loud wrong answer for no answer
    is the other half of this repo's defect class."""
    _, out = _run(monkeypatch, capsys, _meta("cancelled"), LOG_WITH_ERROR_LINE,
                  ["job.py", JOB_ID, "fail"])
    assert "##[error]The operation was canceled." in out


def test_a_cancelled_job_with_no_matches_still_points_somewhere(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _, out = _run(monkeypatch, capsys, _meta("cancelled"), LOG_WITHOUT_ERROR_LINE,
                  ["job.py", JOB_ID, "fail"])
    assert "cancelled" in out
    assert "error-block selection" in out.lower()


# ---------------------------------------------------------------------------
# the failure path must not change
# ---------------------------------------------------------------------------

def test_a_genuine_failure_keeps_the_complete_claim(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """`:fail` is the right selector for a job that failed — do not water it down."""
    _, out = _run(monkeypatch, capsys, _meta("failure"), LOG_WITH_ERROR_LINE,
                  ["job.py", JOB_ID, "fail"])
    assert "All error blocks" in out
    assert "error-block selection" not in out.lower()
