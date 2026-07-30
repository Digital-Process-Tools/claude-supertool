"""#587 — `git-status` still printed the sentence #585 removed from `gh-pr`.

`presets/git/status.py` called `_checks.summarize_github()` on a PR's
`statusCheckRollup` and rendered `Checks: none reported — no check runs on this
commit` for zero runs: one sentence for "the run has not appeared yet" (waiting
is correct) and "no run is ever coming" (waiting is a deadlock).

`git-status` has a fourth state `gh-pr` does not, and it is the reason this was
filed separately. `gh-pr` is handed a PR number; `git-status` resolves a PR *by
branch* while standing in a repo whose local `HEAD` may be ahead of, behind, or
unrelated to the PR's head SHA. Check runs fetched for the PR's head then
describe a different commit than the one the reader is looking at, and any
sentence about "this commit" is about the wrong commit.

These tests pin the *distinctions*, not the tally. A fixture asserting that zero
runs render some absence sentence passes on the broken code. Every test here
fails if the states collapse, if a failed or unavailable lookup can reach the
"never" conclusion, or if the common path grows a network call.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent

_CHECKS_PATH = _ROOT / "presets" / "_checks.py"
_c_spec = importlib.util.spec_from_file_location("checks_587", _CHECKS_PATH)
assert _c_spec is not None and _c_spec.loader is not None
checks = importlib.util.module_from_spec(_c_spec)
_c_spec.loader.exec_module(checks)

_STATUS_PATH = _ROOT / "presets" / "git" / "status.py"
_s_spec = importlib.util.spec_from_file_location("git_status_587", _STATUS_PATH)
assert _s_spec is not None and _s_spec.loader is not None
status = importlib.util.module_from_spec(_s_spec)
_s_spec.loader.exec_module(status)


# ---------------------------------------------------------------------------
# The fourth state, as a pure function — no git, no gh
# ---------------------------------------------------------------------------

_A = "a" * 40
_B = "b" * 40


def test_same_commit_says_nothing() -> None:
    """No note when the PR head *is* your HEAD — the common case, silent."""
    assert checks.head_relation(_A, _A) == ""
    assert checks.head_relation(_A.upper(), _A) == ""


def test_diverged_head_is_stated_and_names_both_commits() -> None:
    """The fourth state: the checks describe a commit that is not your HEAD."""
    line = checks.head_relation(_A, _B, 587)
    assert line
    assert _B[:7] in line
    assert _A[:7] in line
    assert "NOT" in line
    assert "UNKNOWN" not in line
    assert "gh-pr:587" in line


def test_unestablished_relation_declines_and_never_reads_as_a_match() -> None:
    """Silence would read as 'same commit'. An unknown SHA says UNKNOWN."""
    for local, remote in ((_A, None), (None, _B), (None, None), (_A, "HEAD")):
        line = checks.head_relation(local, remote)
        assert line, (local, remote)
        assert "UNKNOWN" in line, (local, remote)


def test_head_relation_rejects_anything_that_is_not_a_full_sha() -> None:
    """A revision *expression* in headRefOid must never be treated as a commit.

    `HEAD`, `master` and friends resolve locally against a commit that is not
    the PR's head — dating that and captioning it as the PR head would be the
    fabrication this issue is about, one layer along.
    """
    for bogus in ("HEAD", "master", "abc", "", "z" * 40, "@{upstream}"):
        assert not checks.is_full_sha(bogus), bogus
    assert checks.is_full_sha(_A)
    assert checks.is_full_sha(_A.upper())


# ---------------------------------------------------------------------------
# The rendered output — real git in tmp_path, fake gh/glab
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str, when: str | None = None) -> str:
    env = dict(os.environ)
    if when:
        env["GIT_AUTHOR_DATE"] = when
        env["GIT_COMMITTER_DATE"] = when
    r = subprocess.run(["git", "-C", str(repo), *args], check=True,
                       capture_output=True, text=True, env=env)
    return r.stdout.strip()


def _init_repo(tmp_path: Path, when: str | None = None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "master")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "f").write_text("x\n")
    _git(repo, "add", "f")
    _git(repo, "commit", "-m", "initial", when=when)
    _git(repo, "checkout", "-b", "fix/587")
    (repo / "g").write_text("y\n")
    _git(repo, "add", "g")
    _git(repo, "commit", "-m", "the work", when=when)
    return repo


class _Calls:
    """Every gh/glab invocation status.py makes — the cost ledger."""

    def __init__(self) -> None:
        self.argv: list[list[str]] = []

    @property
    def gh(self) -> list[list[str]]:
        return [a for a in self.argv if a and a[0] == "gh"]

    @property
    def network(self) -> list[list[str]]:
        return [a for a in self.argv if a and a[0] in ("gh", "glab")]


def _payload(head_oid: str, **over: Any) -> dict:
    base = {
        "number": 587,
        "title": "fix: git-status absence",
        "state": "OPEN",
        "baseRefName": "master",
        "statusCheckRollup": [],
        "headRefOid": head_oid,
        "body": "",
        "additions": 1,
        "deletions": 1,
        "changedFiles": 1,
    }
    base.update(over)
    return base


def _stub(monkeypatch, calls: _Calls, pr: dict | None) -> None:
    real_run = subprocess.run

    def fake_run(args, *a, **kw):
        argv = [str(x) for x in args]
        if argv and argv[0] in ("gh", "glab"):
            calls.argv.append(argv)
            if argv[0] == "gh" and pr is not None:
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout=json.dumps(pr), stderr="")
            return subprocess.CompletedProcess(
                args=args, returncode=1, stdout="", stderr="")
        return real_run(args, *a, **kw)

    monkeypatch.setattr(status.subprocess, "run", fake_run)


def _checks_text(out: str) -> str:
    """Everything after `Checks: ` on the PR line.

    The ambiguity being fixed is the sentence *ending* at "on this commit" —
    `absence()`'s UNKNOWN leg deliberately opens with the same words and then
    goes on to state what is unknown, so `NO_CHECKS not in out` would reject the
    fix. The property to pin is that the Checks text is never that sentence and
    nothing else.
    """
    return next(l for l in out.splitlines() if "Checks: " in l).split("Checks: ")[1]


def _run(repo: Path, monkeypatch, *args: str) -> str:
    monkeypatch.chdir(repo)
    monkeypatch.setattr(status.sys, "argv", ["status.py", *args])
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert status.main() == 0
    return buf.getvalue()


def test_fresh_commit_on_an_open_pr_says_not_yet(tmp_path: Path, monkeypatch) -> None:
    """State 2 — you just pushed. Waiting is right and the line says so."""
    repo = _init_repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    calls = _Calls()
    _stub(monkeypatch, calls, _payload(head))
    out = _run(repo, monkeypatch)
    assert "Checks: none yet" in out
    assert "still expected" in out
    assert _checks_text(out) != checks.NO_CHECKS
    assert "will be created" not in out
    assert "UNKNOWN" not in out


def test_old_commit_on_a_merged_pr_says_none_will_be_created(tmp_path: Path, monkeypatch) -> None:
    """State 3 — the events that could create a run fired and made none."""
    repo = _init_repo(tmp_path, when="2020-01-02T03:04:05 +0000")
    head = _git(repo, "rev-parse", "HEAD")
    calls = _Calls()
    _stub(monkeypatch, calls, _payload(head, state="MERGED"))
    out = _run(repo, monkeypatch)
    assert "none, and none will be created" in out
    assert "MERGED" in out
    assert "none yet" not in out


def test_old_commit_on_an_open_pr_declines(tmp_path: Path, monkeypatch) -> None:
    """Overdue is not decided — an open PR can still receive an event."""
    repo = _init_repo(tmp_path, when="2020-01-02T03:04:05 +0000")
    head = _git(repo, "rev-parse", "HEAD")
    calls = _Calls()
    _stub(monkeypatch, calls, _payload(head))
    out = _run(repo, monkeypatch)
    assert "UNKNOWN" in out
    assert "none will be created" not in out
    assert "none yet" not in out


def test_state_two_and_state_three_are_different_sentences(tmp_path: Path, monkeypatch) -> None:
    """The whole defect, at the rendering layer."""
    fresh = _init_repo(tmp_path / "a")

    old = _init_repo(tmp_path / "b", when="2020-01-02T03:04:05 +0000")
    c1, c2 = _Calls(), _Calls()
    _stub(monkeypatch, c1, _payload(_git(fresh, "rev-parse", "HEAD")))
    a = _run(fresh, monkeypatch)
    _stub(monkeypatch, c2, _payload(_git(old, "rev-parse", "HEAD"), state="MERGED"))
    b = _run(old, monkeypatch)
    a_text = next(l for l in a.splitlines() if "Checks:" in l).split("Checks: ")[1]
    b_text = next(l for l in b.splitlines() if "Checks:" in l).split("Checks: ")[1]
    assert a_text != b_text
    assert checks.NO_CHECKS not in (a_text, b_text)
    assert "none yet" in a_text
    assert "none will be created" in b_text


# --- the fourth state, end to end -----------------------------------------

def test_pr_head_that_is_not_your_head_is_stated(tmp_path: Path, monkeypatch) -> None:
    """A tally for the PR head must not read as a tally for your HEAD."""
    repo = _init_repo(tmp_path)
    pr_head = _git(repo, "rev-parse", "HEAD~1")
    local = _git(repo, "rev-parse", "HEAD")
    calls = _Calls()
    _stub(monkeypatch, calls, _payload(
        pr_head, statusCheckRollup=[{"conclusion": "SUCCESS"}] * 3))
    out = _run(repo, monkeypatch)
    assert "Checks: 3 total: 3 passed" in out
    assert pr_head[:7] in out
    assert local[:7] in out
    assert "NOT" in out


def test_a_pr_head_absent_from_the_local_repo_declines_even_when_merged(
        tmp_path: Path, monkeypatch) -> None:
    """The by-construction guard: no age → no 'never', whatever the PR state.

    A head SHA this clone has never seen cannot be dated locally, and the one
    thing that must not happen is the tool concluding "no run will ever be
    created" from a lookup it could not perform.
    """
    repo = _init_repo(tmp_path)
    calls = _Calls()
    _stub(monkeypatch, calls, _payload("0" * 40, state="MERGED"))
    out = _run(repo, monkeypatch)
    assert "UNKNOWN" in out
    assert "none will be created" not in out
    assert "none yet" not in out
    assert _checks_text(out) != checks.NO_CHECKS


def test_a_revision_expression_in_head_ref_oid_is_not_dated(
        tmp_path: Path, monkeypatch) -> None:
    """`headRefOid: HEAD` resolves locally — to the wrong commit. Decline.

    This is the fabrication the issue warns about: dating the *local* HEAD and
    captioning it as the PR head's age.
    """
    repo = _init_repo(tmp_path)
    calls = _Calls()
    _stub(monkeypatch, calls, _payload("HEAD", state="MERGED"))
    out = _run(repo, monkeypatch)
    assert "UNKNOWN" in out
    assert "none will be created" not in out
    assert "none yet" not in out


def test_missing_head_ref_oid_declines_rather_than_assuming_your_head(
        tmp_path: Path, monkeypatch) -> None:
    """An old `gh` without the field must not silently mean 'same commit'."""
    repo = _init_repo(tmp_path)
    payload = _payload("x")
    del payload["headRefOid"]
    calls = _Calls()
    _stub(monkeypatch, calls, payload)
    out = _run(repo, monkeypatch)
    assert "UNKNOWN" in out
    assert "none will be created" not in out


# --- cost -----------------------------------------------------------------

def test_the_common_path_issues_no_extra_lookup(tmp_path: Path, monkeypatch) -> None:
    """`git-status` is the hottest op. Runs exist → tally, one gh call, done."""
    repo = _init_repo(tmp_path)
    calls = _Calls()
    _stub(monkeypatch, calls, _payload(
        _git(repo, "rev-parse", "HEAD"),
        statusCheckRollup=[{"conclusion": "SUCCESS"}] * 12))
    out = _run(repo, monkeypatch)
    assert "Checks: 12 total: 12 passed" in out
    assert len(calls.gh) == 1, calls.gh
    assert not any("graphql" in " ".join(a) for a in calls.argv)


def test_the_absence_path_issues_no_extra_lookup_either(tmp_path: Path, monkeypatch) -> None:
    """The zero-runs case is *common* here — it is what you run after a push.

    The evidence is local git, so even the uncommon leg pays no network call.
    """
    repo = _init_repo(tmp_path)
    calls = _Calls()
    _stub(monkeypatch, calls, _payload(_git(repo, "rev-parse", "HEAD")))
    out = _run(repo, monkeypatch)
    assert "none yet" in out
    assert len(calls.gh) == 1, calls.gh
    assert not any("graphql" in " ".join(a) for a in calls.argv)


# --- GitLab arm -----------------------------------------------------------

def _glab_stub(monkeypatch, calls: _Calls, mr: dict) -> None:
    real_run = subprocess.run

    def fake_run(args, *a, **kw):
        argv = [str(x) for x in args]
        if argv and argv[0] == "glab":
            calls.argv.append(argv)
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=json.dumps(mr), stderr="")
        if argv and argv[0] == "gh":
            calls.argv.append(argv)
            return subprocess.CompletedProcess(
                args=args, returncode=1, stdout="", stderr="")
        return real_run(args, *a, **kw)

    monkeypatch.setattr(status.subprocess, "run", fake_run)


def test_a_missing_gitlab_pipeline_declines_instead_of_printing_none(
        tmp_path: Path, monkeypatch) -> None:
    """`Pipeline: none` reads as 'no CI here' — the 'never' leg, unearned."""
    repo = _init_repo(tmp_path)
    calls = _Calls()
    _glab_stub(monkeypatch, calls, {
        "iid": 42, "title": "t", "state": "opened",
        "target_branch": "master", "changes_count": "3", "description": "",
    })
    out = _run(repo, monkeypatch)
    line = next(l for l in out.splitlines() if "Pipeline:" in l)
    assert "UNKNOWN" in line
    assert line.strip() != "State: opened | Target: master | Pipeline: none"
    assert "will be created" not in out


def test_an_existing_gitlab_pipeline_status_is_untouched(
        tmp_path: Path, monkeypatch) -> None:
    repo = _init_repo(tmp_path)
    calls = _Calls()
    _glab_stub(monkeypatch, calls, {
        "iid": 42, "title": "t", "state": "opened", "target_branch": "master",
        "changes_count": "3", "description": "",
        "pipeline": {"status": "success"},
    })
    out = _run(repo, monkeypatch)
    assert "Pipeline: success" in out
    assert "UNKNOWN" not in out
