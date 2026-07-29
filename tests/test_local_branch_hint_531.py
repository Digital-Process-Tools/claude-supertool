"""The checkout hint must not be advice on ops you read rather than work on (#531).

`gl-job` prints a branch check in its header, before it knows or cares which
sub-op you asked for, so `gl-job:N:fail` — the op a radar session runs hundreds
of times — ends every call with:

    You are on: max/some-branch ⚠ MISMATCH — switch with: ./supertool 'git-checkout:kevin/other'

Moving HEAD is the one action a monitoring session must never take: fixes go to
a worktree precisely so the working checkout stays put. So the hint is not
merely noise there, it is wrong.

What is dropped is only the *imperative*. The mismatch itself is a fact worth
having and stays on the line, which is what keeps the absence of a checkout
command from ever reading as "you are on the right branch" — the line is
printed either way and always names which of the two states you are in.

`gl-mr` needs no equivalent change and the last test here pins why: its slim
`:status` form returns before the branch check is reached, and its only other
form is the full dashboard you open when you *are* about to work on the branch.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


job = _load("presets/gitlab/job.py", "gitlab_job_531")
mr = _load("presets/gitlab/mr.py", "gitlab_mr_531")

LOCAL = "max/radar-session"
JOB_REF = "kevin/other-branch"


# ---------------------------------------------------------------------------
# the helper
# ---------------------------------------------------------------------------

def _stub_rev_parse(monkeypatch, mod) -> None:
    def fake_run(args, **kw: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args=args, returncode=0,
                                           stdout=LOCAL + "\n", stderr="")
    monkeypatch.setattr(mod.subprocess, "run", fake_run)


def test_read_only_mismatch_states_the_fact_and_withholds_the_command(
    monkeypatch
) -> None:
    _stub_rev_parse(monkeypatch, job)
    line = job._local_branch_check(JOB_REF, actionable=False)
    assert LOCAL in line
    assert "MISMATCH" in line, "the mismatch is signal — only the advice is noise"
    assert JOB_REF in line, "the other branch must still be named"
    assert "git-checkout" not in line
    assert "switch with" not in line


def test_actionable_mismatch_keeps_the_command(monkeypatch) -> None:
    _stub_rev_parse(monkeypatch, job)
    line = job._local_branch_check(JOB_REF, actionable=True)
    assert f"git-checkout:{JOB_REF}" in line


def test_the_check_defaults_to_actionable(monkeypatch) -> None:
    """Silence must never be the default — a new caller gets the old behaviour."""
    _stub_rev_parse(monkeypatch, job)
    assert "git-checkout" in job._local_branch_check(JOB_REF)


@pytest.mark.parametrize("actionable", [True, False])
def test_a_match_reads_identically_either_way(monkeypatch, actionable: bool) -> None:
    """Only the mismatch branch changes; ✓ is a fact with no action attached."""
    _stub_rev_parse(monkeypatch, job)
    line = job._local_branch_check(LOCAL, actionable=actionable)
    assert line == f"You are on: {LOCAL} ✓"


# ---------------------------------------------------------------------------
# gl-job end to end
# ---------------------------------------------------------------------------

def _fake_glab(monkeypatch) -> None:
    meta = json.dumps({
        "name": "test-job", "status": "failed", "stage": "test", "duration": 3.0,
        "web_url": "https://gitlab.example/job/1", "ref": JOB_REF,
        "pipeline": {"id": 999},
    })

    def fake_run(args, **kw: Any) -> subprocess.CompletedProcess:
        if args and args[0] == "git":
            return subprocess.CompletedProcess(args=args, returncode=0,
                                               stdout=LOCAL + "\n", stderr="")
        url = args[2] if len(args) > 2 else ""
        stdout = "boom\nERROR: it broke\n" if url.endswith("/trace") else meta
        return subprocess.CompletedProcess(args=args, returncode=0,
                                           stdout=stdout, stderr="")

    monkeypatch.setattr(job.subprocess, "run", fake_run)


@pytest.mark.parametrize("argv", [
    ["job.py", "123", "raw"],
    ["job.py", "123", "fail"],
    ["job.py", "123", "errors"],
    ["job.py", "123", "grep", "ERROR"],
])
def test_read_only_sub_ops_do_not_advise_a_checkout(monkeypatch, capsys, argv) -> None:
    _fake_glab(monkeypatch)
    monkeypatch.setattr(sys, "argv", argv)
    assert job.main() == 0
    out = capsys.readouterr().out
    assert "MISMATCH" in out, f"{argv[2]} lost the branch state entirely"
    assert "git-checkout" not in out


def test_the_bare_job_view_still_advises_a_checkout(monkeypatch, capsys) -> None:
    """You open a failure to fix it — that is exactly when the hint earns its line."""
    _fake_glab(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["job.py", "123"])
    assert job.main() == 0
    out = capsys.readouterr().out
    assert f"git-checkout:{JOB_REF}" in out


# ---------------------------------------------------------------------------
# gl-mr — pinning the shape that is already right
# ---------------------------------------------------------------------------

def test_gl_mr_status_prints_no_branch_check_at_all(monkeypatch, capsys) -> None:
    """The slim form returns before the check. Pinned so it cannot drift back."""
    payload = json.dumps({
        "iid": 42, "state": "opened", "merge_status": "can_be_merged",
        "has_conflicts": False, "source_branch": JOB_REF, "target_branch": "master",
        "web_url": "https://gitlab.example/mr/42",
    })

    def fake_run(args, **kw: Any) -> subprocess.CompletedProcess:
        if args and args[0] == "git":
            return subprocess.CompletedProcess(args=args, returncode=0,
                                               stdout=LOCAL + "\n", stderr="")
        if args[:2] == ["glab", "api"]:
            return subprocess.CompletedProcess(args=args, returncode=0,
                                               stdout="[]", stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0,
                                           stdout=payload, stderr="")

    monkeypatch.setattr(mr.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["mr.py", "42", "status"])
    assert mr.main() == 0
    out = capsys.readouterr().out
    assert "You are on:" not in out
    assert "git-checkout" not in out
