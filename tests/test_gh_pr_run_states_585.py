"""#585 — "no check runs on this commit" had to mean two opposite things.

`Checks: none reported — no check runs on this commit` was printed both when
GitHub had not created the run yet (waiting is correct) and when no run was
ever coming (waiting is a deadlock). Two readers acted on it ten minutes apart
in the same session and both concluded the wrong one.

These tests pin the *distinction*, not the tally: a fixture whose only
assertion is "zero runs renders some absence sentence" passes on the broken
code. Every test here fails if the three states collapse back into one.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).parent.parent

_CHECKS_PATH = _ROOT / "presets" / "_checks.py"
_c_spec = importlib.util.spec_from_file_location("checks_585", _CHECKS_PATH)
assert _c_spec is not None and _c_spec.loader is not None
checks = importlib.util.module_from_spec(_c_spec)
_c_spec.loader.exec_module(checks)

_PR_PATH = _ROOT / "presets" / "github" / "pr.py"
_p_spec = importlib.util.spec_from_file_location("github_pr_585", _PR_PATH)
assert _p_spec is not None and _p_spec.loader is not None
pr = importlib.util.module_from_spec(_p_spec)
_p_spec.loader.exec_module(pr)


# ---------------------------------------------------------------------------
# The classifier — pure, no gh
# ---------------------------------------------------------------------------

def test_recent_open_pr_says_not_yet_and_never_says_never() -> None:
    """State 2: the run has not appeared yet. Waiting is the right move."""
    text, note = checks.absence("OPEN", 120)
    assert "none yet" in text
    assert "2m" in text
    assert "still expected" in text
    assert "will be created" not in text
    assert "UNKNOWN" not in text
    assert "yet" in note


def test_merged_pr_past_the_window_says_no_run_will_ever_be_created() -> None:
    """State 3: the events that could create a run have fired and made none."""
    text, note = checks.absence("MERGED", 3 * 86400)
    assert "none will be created" in text
    assert "MERGED" in text
    assert "none yet" not in text
    assert "still expected" not in text
    assert "none will be created" in note


def test_closed_pr_names_reopening_as_the_thing_that_would_change_it() -> None:
    text, _ = checks.absence("CLOSED", 7200)
    assert "none will be created" in text
    assert "reopened" in text


def test_state_two_and_state_three_are_not_the_same_sentence() -> None:
    """The whole defect in one assertion."""
    yet, yet_note = checks.absence("OPEN", 60)
    never, never_note = checks.absence("MERGED", 86400)
    assert yet != never
    assert yet_note != never_note
    assert checks.NO_CHECKS not in (yet, never)


def test_unknown_commit_age_declines_instead_of_guessing_either_way() -> None:
    """docs/validators.md: a checker that cannot answer says so."""
    text, note = checks.absence("OPEN", None)
    assert "UNKNOWN" in text
    assert "none will be created" not in text
    assert "none yet" not in text
    assert "still expected" not in text
    assert "UNKNOWN" in note


def test_open_pr_long_past_the_window_declines_rather_than_claiming_never() -> None:
    """Overdue is not proof. An open PR can still receive an event."""
    text, _ = checks.absence("OPEN", 7200)
    assert "UNKNOWN" in text
    assert "2h" in text
    assert "none will be created" not in text


def test_unrecognised_pr_state_declines() -> None:
    text, _ = checks.absence(None, 86400)
    assert "UNKNOWN" in text
    assert "none will be created" not in text


def test_a_just_merged_pr_inside_the_window_is_still_not_yet() -> None:
    """Grace covers creation latency; it is not keyed on the PR being open."""
    text, _ = checks.absence("MERGED", 30)
    assert "none yet" in text
    assert "none will be created" not in text


def test_grace_window_is_named_and_generous() -> None:
    """Measured worst case in #585 was 4.5min; the window must clear it."""
    assert checks.CHECK_CREATION_GRACE_SECS >= 300
    text, _ = checks.absence("OPEN", 10)
    assert f"{checks.CHECK_CREATION_GRACE_SECS // 60}min" in text


def test_absence_never_returns_the_old_ambiguous_line_for_a_decided_state() -> None:
    for pr_state, age in (("OPEN", 5), ("MERGED", 99999), ("CLOSED", 99999)):
        text, _ = checks.absence(pr_state, age)
        assert text != checks.NO_CHECKS, (pr_state, age)


def test_tally_path_is_untouched() -> None:
    assert checks.summarize(["SUCCESS", "SUCCESS"]) == "2 total: 2 passed, 0 failed, 0 pending"
    assert checks.summarize([]) == checks.NO_CHECKS


# ---------------------------------------------------------------------------
# pr.main() — the rendered lines
# ---------------------------------------------------------------------------

def _iso_ago(secs: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=secs)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _payload(**overrides: Any) -> dict:
    base = {
        "number": 585,
        "title": "fix: three run states",
        "state": "OPEN",
        "author": {"login": "max"},
        "headRefName": "fix/585",
        "baseRefName": "master",
        "labels": [],
        "milestone": None,
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "reviewDecision": None,
        "reviews": [],
        "mergeCommit": None,
        "additions": 1,
        "deletions": 1,
        "changedFiles": 1,
        "statusCheckRollup": [],
        "url": "https://github.com/Digital-Process-Tools/claude-supertool/pull/585",
        "body": "",
        "comments": [],
        "assignees": [],
        "createdAt": _iso_ago(300),
        "updatedAt": _iso_ago(300),
    }
    base.update(overrides)
    return base


def _ok(stdout: str) -> Any:
    return subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=stdout, stderr="")


def _fail() -> Any:
    return subprocess.CompletedProcess(args=["gh"], returncode=1, stdout="", stderr="boom")


class _Gh:
    """Fake `gh`, dispatching on the argv it is handed.

    `age_secs=None` makes the commit-age lookup fail, which is the
    cannot-establish case.
    """

    def __init__(self, payload: dict, age_secs: int | None = 90) -> None:
        self.payload = payload
        self.age_secs = age_secs
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> Any:
        self.calls.append(list(args))
        joined = " ".join(str(a) for a in args)
        if args and args[0] == "git":
            return _fail()
        if "committedDate" in joined:
            if self.age_secs is None:
                return _fail()
            return _ok(json.dumps({"data": {"repository": {"pullRequest": {
                "commits": {"nodes": [{"commit": {
                    "oid": "deadbeef", "pushedDate": None,
                    "committedDate": _iso_ago(self.age_secs),
                }}]}
            }}}}))
        if "reviewThreads" in joined:
            return _ok(json.dumps({"data": {"repository": {"pullRequest": {
                "reviewThreads": {"nodes": []}}}}}))
        return _ok(json.dumps(self.payload))

    @property
    def asked_for_commit_age(self) -> bool:
        return any("committedDate" in " ".join(str(a) for a in c) for c in self.calls)


def _run(monkeypatch, gh: _Gh, argv: list[str]) -> str:
    monkeypatch.setattr(pr.subprocess, "run", gh)
    monkeypatch.setattr(sys, "argv", argv)
    assert pr.main() == 0
    return ""


def test_full_mode_recent_open_pr_renders_not_yet(monkeypatch, capsys) -> None:
    gh = _Gh(_payload(), age_secs=90)
    _run(monkeypatch, gh, ["pr.py", "585"])
    out = capsys.readouterr().out
    assert "Checks: none yet" in out
    assert "no check runs on this commit\n" not in out
    assert "Mergeable: yes (no merge conflicts) — no checks yet" in out
    assert "will be created" not in out


def test_full_mode_merged_pr_renders_never(monkeypatch, capsys) -> None:
    gh = _Gh(_payload(state="MERGED", mergeable="MERGEABLE"), age_secs=4 * 86400)
    _run(monkeypatch, gh, ["pr.py", "585"])
    out = capsys.readouterr().out
    assert "none will be created" in out
    assert "none yet" not in out
    assert "Mergeable: yes (no merge conflicts) — no checks, and none will be created" in out


def test_full_mode_declines_when_the_age_lookup_fails(monkeypatch, capsys) -> None:
    """A failed lookup must not render as state 3. That is the same bug, deeper."""
    gh = _Gh(_payload(state="MERGED"), age_secs=None)
    _run(monkeypatch, gh, ["pr.py", "585"])
    out = capsys.readouterr().out
    assert "UNKNOWN" in out
    assert "none will be created" not in out
    assert "none yet" not in out


def test_slim_mode_distinguishes_too(monkeypatch, capsys) -> None:
    gh = _Gh(_payload(), age_secs=45)
    _run(monkeypatch, gh, ["pr.py", "585", "status"])
    out = capsys.readouterr().out
    assert "checks: none yet" in out


def test_slim_mode_merged_says_never(monkeypatch, capsys) -> None:
    gh = _Gh(_payload(state="MERGED"), age_secs=86400)
    _run(monkeypatch, gh, ["pr.py", "585", "status"])
    out = capsys.readouterr().out
    assert "none will be created" in out


def test_no_extra_lookup_when_check_runs_exist(monkeypatch, capsys) -> None:
    """Cost pin: the hot path (runs exist) must not pay for the third state."""
    gh = _Gh(_payload(statusCheckRollup=[{"conclusion": "SUCCESS"}] * 12), age_secs=90)
    _run(monkeypatch, gh, ["pr.py", "585", "status"])
    out = capsys.readouterr().out
    assert "12 total: 12 passed" in out
    assert not gh.asked_for_commit_age


def test_full_mode_tally_unchanged_when_runs_exist(monkeypatch, capsys) -> None:
    gh = _Gh(_payload(statusCheckRollup=[
        {"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"},
    ]), age_secs=90)
    _run(monkeypatch, gh, ["pr.py", "585"])
    out = capsys.readouterr().out
    assert "Checks: 2 total: 1 passed, 1 failed, 0 pending" in out
    assert not gh.asked_for_commit_age
