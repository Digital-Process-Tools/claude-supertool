"""#1110 finding 2: the boilerplate discount is substring-matchable (misreports).

`presets/gitlab/job.py`'s `_is_boilerplate` used `rx.search(line)`, so a marker
substring appearing ANYWHERE in a line -- not just a line that IS the runner's
own teardown noise -- marked the whole line boilerplate. A failing test whose
own assertion message happens to quote `Cleaning up project directory` (or
`section_start:...`, or `ERROR: Job failed: exit code N`) got discounted the
same as GitLab's own terminal lines, and `boilerplate_only` then asserted "no
cause found" over a log that names one.

Positive control: `LOG_BOILERPLATE_ONLY` below is copied from
`tests/test_gl_job_fail_honesty_1095_1097.py`'s own fixture of the same name --
real GitLab teardown noise, unmodified -- so the fix must not stop recognising
the genuine case while it stops recognising the fake one.
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


gl_job = _load("gitlab/job.py", "gitlab_job_1110")

GL_ID = "7125000"
_REAL_RUN = subprocess.run

# Real GitLab teardown noise -- the positive control. Must still discount.
LOG_BOILERPLATE_ONLY = "\n".join([
    "Running with gitlab-runner 17.1.0",
    "section_start:1750000000:step_script",
    "$ npx playwright test",
    "  12 passed (48.1s)",
    "section_end:1750000060:step_script",
    "section_start:1750000060:cleanup_file_variables",
    "Cleaning up project directory and file based variables",
    "section_end:1750000061:cleanup_file_variables",
    "ERROR: Job failed: exit code 1",
])

# A failing assertion whose OWN message text quotes the marker substrings --
# the negative control. Must NOT discount: this names a real cause.
LOG_MARKER_EMBEDDED_IN_ASSERTION = "\n".join([
    "Running with gitlab-runner 17.1.0",
    "section_start:1750000000:step_script",
    "$ npx playwright test",
    "  1) testFoo",
    "ERROR: assertion failed in testFoo (Cleaning up project directory)",
    "section_end:1750000060:step_script",
    "Cleaning up project directory and file based variables",
    "ERROR: Job failed: exit code 1",
])


def _gl_meta(status: str) -> dict[str, Any]:
    return {
        "id": int(GL_ID), "name": "conformity_prepare", "status": status,
        "stage": "prepare", "duration": 12.0,
        "web_url": f"https://gitlab.example/-/jobs/{GL_ID}",
        "ref": "", "pipeline": {"id": 999},
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


def _run_gl(monkeypatch, capsys, status, log) -> tuple[int, str]:
    monkeypatch.setattr(sys, "argv", ["job.py", GL_ID, "fail"])
    monkeypatch.setattr(gl_job.subprocess, "run", _fake_glab(_gl_meta(status), log))
    rc = gl_job.main()
    return rc, capsys.readouterr().out


def test_real_teardown_noise_is_still_discounted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Positive control: the genuine case must still fire."""
    _, out = _run_gl(monkeypatch, capsys, "failed", LOG_BOILERPLATE_ONLY)
    assert "All error blocks" not in out
    assert "pattern is missing here" in out


def test_a_marker_quoted_inside_a_real_assertion_is_not_boilerplate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Negative control: the fake case, from #1110's own repro, must not fire.

    The assertion line names a real cause -- `assertion failed in testFoo` --
    and merely quotes a boilerplate substring inside its own parenthetical.
    `boilerplate_only` must not swallow it.
    """
    _, out = _run_gl(monkeypatch, capsys, "failed", LOG_MARKER_EMBEDDED_IN_ASSERTION)
    assert "All error blocks" in out, (
        "a line containing a real assertion failure was discounted as pure "
        "teardown noise because it happens to quote a boilerplate substring"
    )
    assert "assertion failed in testFoo" in out
