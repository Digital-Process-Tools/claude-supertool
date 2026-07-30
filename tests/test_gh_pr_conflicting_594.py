"""#594 — a CONFLICTING PR gets zero check runs forever, and we said "wait".

`pull_request` workflows run against `refs/pull/N/merge`, a ref GitHub builds by
merging head into base. It cannot be built while the merge conflicts, so a
`CONFLICTING` PR receives *no check runs at all*, permanently. `absence()` had
no vocabulary for that: the head commit is past the creation window and the PR
is open, so the reading landed in the UNKNOWN leg — "an event could still fire,
check the Checks tab" — which tells the reader to wait or go look when the only
thing that changes this is a rebase.

These tests pin the *distinction*, not the presence of the word "conflict". A
fixture asserting "the output mentions a conflict somewhere" passes on code that
never distinguishes anything. Every test here fails if the CONFLICTING leg
collapses into one of the three that were already there — and the pair of
identity tests fail if an *unresolved* `mergeable` state (GitHub's `UNKNOWN`,
returned while it recomputes mergeability) is allowed to change any wording at
all, in either direction.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent

_CHECKS_PATH = _ROOT / "presets" / "_checks.py"
_c_spec = importlib.util.spec_from_file_location("checks_594", _CHECKS_PATH)
assert _c_spec is not None and _c_spec.loader is not None
checks = importlib.util.module_from_spec(_c_spec)
_c_spec.loader.exec_module(checks)

_PR_PATH = _ROOT / "presets" / "github" / "pr.py"
_p_spec = importlib.util.spec_from_file_location("github_pr_594", _PR_PATH)
assert _p_spec is not None and _p_spec.loader is not None
pr = importlib.util.module_from_spec(_p_spec)
_p_spec.loader.exec_module(pr)


# ---------------------------------------------------------------------------
# The classifier — pure, no gh
# ---------------------------------------------------------------------------

def test_conflicting_says_rebase_and_never_says_wait() -> None:
    """The state that was missing: waiting is a deadlock, rebasing is the fix."""
    text, note = checks.absence("OPEN", 7200, mergeable="CONFLICTING")
    assert "CONFLICTING" in text
    assert "Rebase" in text
    assert "waiting will not change this" in text
    assert "none yet" not in text
    assert "still expected" not in text
    assert "Check the PR's Checks tab" not in text
    assert "rebase" in note
    assert "CONFLICTING" in note


def test_conflicting_names_the_ref_that_cannot_be_built() -> None:
    """The evidence, not a vibe: no `refs/pull/N/merge`, so nothing can run."""
    text, _ = checks.absence("OPEN", 7200, mergeable="CONFLICTING")
    assert "refs/pull" in text
    assert "pull_request" in text


def test_conflicting_wins_over_the_grace_window() -> None:
    """A just-pushed conflicting head is not "still expected" — it is stuck."""
    text, _ = checks.absence("OPEN", 5, mergeable="CONFLICTING")
    assert "CONFLICTING" in text
    assert "none yet" not in text
    assert "still expected" not in text


def test_conflicting_wins_over_an_unestablished_age() -> None:
    """The conflict is established evidence; the age lookup is not needed."""
    text, _ = checks.absence("OPEN", None, mergeable="CONFLICTING")
    assert "CONFLICTING" in text
    assert "could not establish" not in text


def test_all_four_states_are_four_different_sentences() -> None:
    """The whole defect in one assertion, extended to the fourth leg."""
    yet = checks.absence("OPEN", 60)
    never = checks.absence("MERGED", 86400)
    unknown = checks.absence("OPEN", 7200)
    conflict = checks.absence("OPEN", 7200, mergeable="CONFLICTING")
    texts = [yet[0], never[0], unknown[0], conflict[0]]
    notes = [yet[1], never[1], unknown[1], conflict[1]]
    assert len(set(texts)) == 4, texts
    assert len(set(notes)) == 4, notes
    assert checks.NO_CHECKS not in texts


def test_case_and_whitespace_do_not_hide_a_conflict() -> None:
    expected = checks.absence("OPEN", 7200, mergeable="CONFLICTING")
    for variant in ("conflicting", " Conflicting ", "CONFLICTING\n"):
        assert checks.absence("OPEN", 7200, mergeable=variant) == expected, variant


# --- the identity tests: an unresolved mergeable state changes nothing ------

_CASES = (("OPEN", 60), ("OPEN", 7200), ("MERGED", 86400), ("CLOSED", 99999),
          ("OPEN", None), (None, 86400))


def test_unknown_mergeable_is_byte_identical_to_no_mergeable_info() -> None:
    """GitHub returns `UNKNOWN` while it recomputes mergeability.

    That is not evidence of a conflict *or* of the absence of one, so it must
    reach exactly the wording it reached before this parameter existed. Pinned
    as equality rather than as "does not say rebase", because the failure mode
    worth preventing is a confident claim in *either* direction.
    """
    for state, age in _CASES:
        assert (checks.absence(state, age, mergeable="UNKNOWN")
                == checks.absence(state, age)), (state, age)


def test_no_other_mergeable_value_changes_the_wording_either() -> None:
    for value in (None, "", "   ", "MERGEABLE", "BEHIND", "SOMETHING_NEW", 0):
        for state, age in _CASES:
            assert (checks.absence(state, age, mergeable=value)
                    == checks.absence(state, age)), (value, state, age)


def test_the_old_three_legs_never_mention_conflicts() -> None:
    """The old logic claims nothing about conflicts — that is what makes the
    exact-match design safe for an unresolved mergeable state."""
    for state, age in _CASES:
        text, note = checks.absence(state, age)
        assert "CONFLICT" not in text.upper(), (state, age)
        assert "REBASE" not in text.upper(), (state, age)
        assert "CONFLICT" not in note.upper(), (state, age)


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
        "number": 594,
        "title": "fix: conflicting PRs never get checks",
        "state": "OPEN",
        "author": {"login": "max"},
        "headRefName": "fix/594",
        "baseRefName": "master",
        "labels": [],
        "milestone": None,
        "isDraft": False,
        "mergeable": "CONFLICTING",
        "reviewDecision": None,
        "reviews": [],
        "mergeCommit": None,
        "additions": 1,
        "deletions": 1,
        "changedFiles": 1,
        "statusCheckRollup": [],
        "url": "https://github.com/Digital-Process-Tools/claude-supertool/pull/594",
        "body": "",
        "comments": [],
        "assignees": [],
        "createdAt": _iso_ago(7200),
        "updatedAt": _iso_ago(7200),
    }
    base.update(overrides)
    return base


def _ok(stdout: str) -> Any:
    return subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=stdout, stderr="")


def _fail() -> Any:
    return subprocess.CompletedProcess(args=["gh"], returncode=1, stdout="", stderr="boom")


class _Gh:
    """Fake `gh`, dispatching on the argv it is handed. Also the cost ledger."""

    def __init__(self, payload: dict, age_secs: int | None = 7200) -> None:
        self.payload = payload
        self.age_secs = age_secs
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> Any:
        self.calls.append([str(a) for a in args])
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
    def gh_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c and c[0] == "gh"]

    @property
    def asked_for_commit_age(self) -> bool:
        return any("committedDate" in " ".join(c) for c in self.calls)


def _run(monkeypatch, gh: _Gh, argv: list[str]) -> None:
    monkeypatch.setattr(pr.subprocess, "run", gh)
    monkeypatch.setattr(sys, "argv", argv)
    assert pr.main() == 0


def _checks_text(out: str) -> str:
    return next(l for l in out.splitlines() if l.startswith("Checks: ")).split("Checks: ")[1]


def test_full_mode_conflicting_pr_says_rebase(monkeypatch, capsys) -> None:
    gh = _Gh(_payload())
    _run(monkeypatch, gh, ["pr.py", "594"])
    out = capsys.readouterr().out
    text = _checks_text(out)
    assert "CONFLICTING" in text
    assert "Rebase" in text
    assert "Check the PR's Checks tab" not in text
    assert "none yet" not in out


def test_full_mode_conflicts_line_carries_the_same_note(monkeypatch, capsys) -> None:
    """`Conflicts:` and `Checks:` come from one call and cannot disagree (#585)."""
    gh = _Gh(_payload())
    _run(monkeypatch, gh, ["pr.py", "594"])
    out = capsys.readouterr().out
    line = next(l for l in out.splitlines() if l.startswith("Conflicts: "))
    assert line == ("Conflicts: YES — cannot merge — no checks, and none will "
                    "be created (mergeable is CONFLICTING) — rebase")


def test_full_mode_a_mergeable_pr_is_unchanged(monkeypatch, capsys) -> None:
    """The regression guard: the fourth leg must not leak onto healthy PRs."""
    gh = _Gh(_payload(mergeable="MERGEABLE"))
    _run(monkeypatch, gh, ["pr.py", "594"])
    out = capsys.readouterr().out
    assert "CONFLICTING" not in out
    assert "rebase" not in out
    assert "UNKNOWN" in _checks_text(out)


def test_full_mode_an_unresolved_mergeable_state_is_unchanged(monkeypatch, capsys) -> None:
    gh_unknown = _Gh(_payload(mergeable="UNKNOWN"))
    _run(monkeypatch, gh_unknown, ["pr.py", "594"])
    unknown_out = capsys.readouterr().out
    assert "rebase" not in unknown_out
    assert "CONFLICTING" not in unknown_out
    assert "UNKNOWN" in _checks_text(unknown_out)


def test_slim_mode_conflicting_says_rebase_too(monkeypatch, capsys) -> None:
    gh = _Gh(_payload())
    _run(monkeypatch, gh, ["pr.py", "594", "status"])
    out = capsys.readouterr().out
    assert "conflicts: yes" in out
    checks_line = next(l for l in out.splitlines() if l.startswith("checks: "))
    assert "CONFLICTING" in checks_line
    assert "Rebase" in checks_line


# --- cost ------------------------------------------------------------------

def test_conflicting_costs_no_call_a_mergeable_pr_does_not_also_pay(
        monkeypatch, capsys) -> None:
    """`mergeable` already rides the `gh pr view` call — the leg is free.

    Pinned as an equality against the pre-existing absence path rather than as
    an absolute count, because that is the actual claim: this fix added no
    request anywhere.
    """
    conflicting = _Gh(_payload())
    _run(monkeypatch, conflicting, ["pr.py", "594", "status"])
    capsys.readouterr()
    mergeable = _Gh(_payload(mergeable="MERGEABLE"))
    _run(monkeypatch, mergeable, ["pr.py", "594", "status"])
    capsys.readouterr()
    assert len(conflicting.calls) == len(mergeable.calls), (
        conflicting.calls, mergeable.calls)


def test_no_extra_lookup_when_check_runs_exist_even_when_conflicting(
        monkeypatch, capsys) -> None:
    """Cost pin: the hot path (runs exist) never reaches `absence()` at all."""
    gh = _Gh(_payload(statusCheckRollup=[{"conclusion": "SUCCESS"}] * 12))
    _run(monkeypatch, gh, ["pr.py", "594", "status"])
    out = capsys.readouterr().out
    assert "12 total: 12 passed" in out
    assert not gh.asked_for_commit_age


def test_mergeable_is_already_in_the_pr_view_field_list(monkeypatch, capsys) -> None:
    """No second request for it — it is a field on one already being made."""
    gh = _Gh(_payload())
    _run(monkeypatch, gh, ["pr.py", "594", "status"])
    capsys.readouterr()
    views = [c for c in gh.gh_calls if "view" in c]
    assert len(views) == 1, gh.gh_calls
    assert "mergeable" in " ".join(views[0])
