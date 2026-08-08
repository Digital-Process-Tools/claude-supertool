"""#1083 — `gh-branch` took a branch, and answered confidently for a SHA.

The release gate's own sentence is *"the default branch is green at leg level
**for the exact commit being tagged**"*. That is a question about a commit,
deliberately, because the head can move between the check and the tag. There
was no op that took one.

There was, however, an op that *accepted* one. `gh-branch:<40 hex>` resolved the
SHA through `gh api commits/<ref>` — which works for a SHA — and then asked
`gh run list --branch <40 hex>`, which matches no branch and returns `[]` with
exit 0. Zero runs became `NO RUN — zero workflow runs on 412375a`, printed
against a commit that had two runs and eighteen legs. Measured on
412375ae98ab102ac33fe3f2bcce109243990030 on 2026-08-08.

So this is not a missing feature with a clean error in front of it. It is the
house defect — an absence produced by the tool, rendered as an absence in the
world — inside the op written to stop exactly that.

The other half is the one that bit the maintainer by hand: `gh run list
--commit` takes a **full** object name and returns `[]`, exit 0, for a prefix.
`gh run list --commit 412375a` → `[]`; `--commit 412375ae98…` → two runs. That
is why the resolved 40-hex SHA, and never the caller's argument, reaches the
run list.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent

SHA = "412375ae98ab102ac33fe3f2bcce109243990030"
SHORT = SHA[:7]


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


branch = _load("presets/github/branch.py", "github_branch_1083")


class _Completed:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


RUNS = [
    {"workflowName": "tests", "headSha": SHA, "databaseId": 31265476147,
     "status": "completed", "conclusion": "success", "event": "push",
     "createdAt": "2026-08-08T10:00:00Z", "attempt": 1},
    {"workflowName": "CodeQL", "headSha": SHA, "databaseId": 31265475965,
     "status": "completed", "conclusion": "success", "event": "dynamic",
     "createdAt": "2026-08-08T10:00:00Z", "attempt": 1},
]

JOBS = {"jobs": [{"name": "pytest", "status": "completed",
                  "conclusion": "success", "databaseId": 1}]}


class _Gh:
    """`gh`, with the two silent-empty behaviours GitHub actually has.

    A run list asked by `--branch <sha>` answers `[]`, and so does one asked by
    `--commit <prefix>`. Both are exit 0. A mock that returned the runs
    regardless would let the fix pass without being the fix.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv, *a, **kw):
        argv = list(argv)
        self.calls.append(argv)
        joined = " ".join(argv)
        if "repo view" in joined:
            return _Completed(json.dumps(
                {"nameWithOwner": "Digital-Process-Tools/claude-supertool",
                 "defaultBranchRef": {"name": "master"}}))
        if "/commits/" in joined and "check-runs" not in joined:
            return _Completed(json.dumps(
                {"sha": SHA,
                 "commit": {"committer": {"date": "2026-08-08T10:00:00Z"}}}))
        if "run list" in joined:
            if "--branch" in argv:
                return _Completed("[]")
            if "--commit" in argv:
                given = argv[argv.index("--commit") + 1]
                if given == SHA:
                    return _Completed(json.dumps(RUNS))
                return _Completed("[]")
            return _Completed("[]")
        if "run view" in joined:
            return _Completed(json.dumps(JOBS))
        return _Completed("", returncode=1, stderr="404 not found")

    def run_list_argv(self) -> list[str]:
        for argv in self.calls:
            if "list" in argv and "run" in argv:
                return argv
        return []


def _render(monkeypatch, capsys, ref: str, gh: _Gh) -> str:
    monkeypatch.setattr(branch.subprocess, "run", gh)
    monkeypatch.setattr(branch._declared_legs.subprocess, "run", gh)
    monkeypatch.setattr(branch._declared_workflows.subprocess, "run", gh)
    monkeypatch.setattr(sys, "argv", ["branch.py", ref])
    branch.main()
    return capsys.readouterr().out


# ---------------------------------------------------------------------------
# which question was asked
# ---------------------------------------------------------------------------

def test_a_full_sha_is_read_as_a_commit() -> None:
    assert branch.ref_mode(SHA, SHA) == branch.MODE_COMMIT


def test_an_abbreviated_sha_is_read_as_a_commit() -> None:
    assert branch.ref_mode(SHORT, SHA) == branch.MODE_COMMIT


def test_a_branch_name_is_read_as_a_branch() -> None:
    assert branch.ref_mode("master", SHA) == branch.MODE_BRANCH


def test_a_hex_shaped_branch_name_is_read_as_a_branch() -> None:
    """`deadbee` is a legal branch name. It is only a commit if it resolves to
    a SHA that starts with it — which is what the resolution already tells us,
    so no second call and no ambiguity refusal is needed to find out."""
    assert branch.ref_mode("deadbee", SHA) == branch.MODE_BRANCH


def test_a_ref_too_short_to_abbreviate_a_sha_is_a_branch() -> None:
    assert branch.ref_mode("41", SHA) == branch.MODE_BRANCH


def test_case_does_not_decide_it() -> None:
    assert branch.ref_mode(SHORT.upper(), SHA) == branch.MODE_COMMIT


# ---------------------------------------------------------------------------
# the run list is asked the question it can answer
# ---------------------------------------------------------------------------

def test_a_commit_is_never_asked_for_by_branch(monkeypatch, capsys) -> None:
    gh = _Gh()
    _render(monkeypatch, capsys, SHA, gh)
    argv = gh.run_list_argv()
    assert "--branch" not in argv, (
        "`gh run list --branch <sha>` matches no branch and returns [] with "
        f"exit 0 — that is the whole bug. argv was {argv}")
    assert "--commit" in argv


def test_the_full_sha_reaches_the_run_list_not_the_abbreviation(
        monkeypatch, capsys) -> None:
    gh = _Gh()
    _render(monkeypatch, capsys, SHORT, gh)
    argv = gh.run_list_argv()
    assert argv[argv.index("--commit") + 1] == SHA, (
        "`gh run list --commit` returns [] exit 0 for an abbreviation; the "
        "resolved 40-hex name is the only safe thing to pass")


def test_a_branch_is_still_asked_for_by_branch(monkeypatch, capsys) -> None:
    gh = _Gh()
    _render(monkeypatch, capsys, "master", gh)
    argv = gh.run_list_argv()
    assert "--branch" in argv and "--commit" not in argv


# ---------------------------------------------------------------------------
# the render says which thing it answered for
# ---------------------------------------------------------------------------

def test_the_verdict_line_names_a_commit(monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, SHA, _Gh())
    assert f"Commit {SHORT}:" in out
    assert f"Branch {SHA}:" not in out, (
        "a SHA rendered as a branch name is the mislabel that made the wrong "
        "answer readable")


def test_the_runs_on_the_commit_are_found(monkeypatch, capsys) -> None:
    """The regression proper: this used to print NO RUN for a commit with runs."""
    out = _render(monkeypatch, capsys, SHA, _Gh())
    assert "NO RUN" not in out
    assert "Legs:" in out
    assert "CodeQL" in out and "tests" in out


def test_commit_mode_declines_the_previous_head_evidence(
        monkeypatch, capsys) -> None:
    """`--commit` returns one commit's runs, so "ran last time, not this time"
    has no second commit to compare against. Branch mode has that evidence;
    commit mode must say it does not rather than let its silence read as
    "nothing was missing"."""
    out = _render(monkeypatch, capsys, SHA, _Gh())
    assert "previous head" in out.lower()
    assert "UNKNOWN" in out or "not available" in out


def test_branch_mode_is_untouched(monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, "master", _Gh())
    assert "Branch master:" in out
    assert "Commit " not in out.splitlines()[1]
