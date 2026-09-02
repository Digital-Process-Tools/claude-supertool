"""#2117 — the declared-workflow-with-no-run read has no first-run window.

`gh-pr-merge:375:squash|force|cleanup` merged, then read the default branch
five seconds later and said a workflow with a push trigger was "declared ...
with no run on it ... UNKNOWN from here" — the state the oss plugin's
release gate treats as blocking. A `radar` read ninety seconds later, same
commit, showed the run in progress: GitHub simply had not created it yet.

The check-run reading (`_checks.absence()`) already has exactly this
concept — `CHECK_CREATION_GRACE_SECS`, the measured window in which a first
run has always appeared, on this same forge. `undispatched_lines` /
`scope_for` had no equivalent: a young commit and an old one produced the
same sentence.

Pinned here: inside the window a push-triggered workflow with no run yet
reads as "still expected", the same vocabulary `verdict()`'s own
`missing`-workflow branch already uses one function up in this file; past
the window nothing changes; an unreadable `on:` block is not touched by the
window at all, because a commit's age says nothing about whether a file
could be parsed.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location(
    "github_branch_2117", _ROOT / "presets" / "github" / "branch.py")
assert _spec is not None and _spec.loader is not None
branch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(branch)

_PUSH_WF = {"name": "CI", "path": ".github/workflows/ci.yml",
            "triggers": ["push", "pull_request"]}
_UNREADABLE_WF = {"name": "mystery", "path": ".github/workflows/mystery.yml",
                  "triggers": None}


def _loud_line(lines: list[str], name: str) -> str:
    for line in lines:
        if name in line:
            return line
    raise AssertionError(f"{name!r} not named in {lines}")


# ---------------------------------------------------------------------------
# inside the window: still expected, not UNKNOWN
# ---------------------------------------------------------------------------

def test_inside_the_window_a_push_workflow_reads_as_still_expected() -> None:
    lines = branch.undispatched_lines([_PUSH_WF], age_secs=5,
                                      grace=branch._GRACE)
    line = _loud_line(lines, "CI")
    assert "still expected" in line, line
    assert "UNKNOWN" not in line, (
        f"a commit inside the creation window must not read as the same "
        f"open question as one well past it: {line}")


def test_inside_the_window_the_commit_age_and_window_are_named() -> None:
    lines = branch.undispatched_lines([_PUSH_WF], age_secs=5,
                                      grace=branch._GRACE)
    line = _loud_line(lines, "CI")
    assert "5s" in line or "5 s" in line.replace("s ", "s "), line
    assert branch._window(branch._GRACE) in line, line


def test_scope_for_does_not_count_a_waiting_workflow_as_unresolved(monkeypatch) -> None:
    """`unresolved` is what a caller reads to decide whether this is a real
    open question. A workflow still inside its creation window is not one."""
    declared_pair = ([_PUSH_WF], "")
    monkeypatch.setattr(branch, "workflow_names", lambda selected: ["other"])
    _clause, lines, unresolved = branch.scope_for(
        "o/r", "a" * 40, {"other": {}}, declared_pair=declared_pair,
        age_secs=5, grace=branch._GRACE)
    assert unresolved == "", (
        f"a workflow still inside its creation window was counted as an "
        f"open question: {unresolved!r} — {lines}")
    assert lines, "the waiting workflow must still be named, just not as open"


def test_scope_clause_itself_is_not_left_alarming_for_a_waiting_workflow(monkeypatch) -> None:
    """`scope_clause` is the sentence glued onto the `Verdict:` line that
    `main()` and the dashboard print unconditionally — a reader who reads
    only the headline must not see the pre-#2117 "NOT covered" wording with
    no hint that the workflow is merely early. Caught reviewing #2117 itself:
    `unresolved` and `undispatched_lines` were fixed, `scope_clause` was not.
    """
    declared_pair = ([_PUSH_WF], "")
    monkeypatch.setattr(branch, "workflow_names", lambda selected: ["other"])
    clause, _lines, _unresolved = branch.scope_for(
        "o/r", "a" * 40, {"other": {}}, declared_pair=declared_pair,
        age_secs=5, grace=branch._GRACE)
    assert "still" in clause and "window" in clause, (
        f"the headline scope clause gives no hint the workflow is merely "
        f"inside its creation window: {clause!r}")


def test_scope_clause_is_unchanged_when_nothing_is_waiting(monkeypatch) -> None:
    """The #846 wording, byte for byte, when the window does not apply."""
    declared_pair = ([_PUSH_WF], "")
    monkeypatch.setattr(branch, "workflow_names", lambda selected: ["other"])
    clause, _lines, _unresolved = branch.scope_for(
        "o/r", "a" * 40, {"other": {}}, declared_pair=declared_pair)
    assert "still" not in clause and "window" not in clause, clause


# ---------------------------------------------------------------------------
# past the window / age unknown: unchanged
# ---------------------------------------------------------------------------

def test_past_the_window_the_open_question_wording_is_unchanged() -> None:
    lines = branch.undispatched_lines([_PUSH_WF], age_secs=branch._GRACE + 1,
                                      grace=branch._GRACE)
    line = _loud_line(lines, "CI")
    assert "UNKNOWN from here" in line, line
    assert "still expected" not in line, line


def test_unknown_age_the_open_question_wording_is_unchanged() -> None:
    lines = branch.undispatched_lines([_PUSH_WF], age_secs=None,
                                      grace=branch._GRACE)
    line = _loud_line(lines, "CI")
    assert "UNKNOWN from here" in line, line


def test_no_age_argument_at_all_is_unchanged_default_behaviour() -> None:
    """A caller that has not been updated to pass `age_secs` gets exactly
    today's behaviour — the default must not silently start declaring
    every no-age call to be inside the window."""
    lines = branch.undispatched_lines([_PUSH_WF])
    line = _loud_line(lines, "CI")
    assert "UNKNOWN from here" in line, line


def test_scope_for_still_counts_unresolved_past_the_window(monkeypatch) -> None:
    declared_pair = ([_PUSH_WF], "")
    monkeypatch.setattr(branch, "workflow_names", lambda selected: ["other"])
    _clause, _lines, unresolved = branch.scope_for(
        "o/r", "a" * 40, {"other": {}}, declared_pair=declared_pair,
        age_secs=branch._GRACE + 1, grace=branch._GRACE)
    assert unresolved, "a push workflow with no run, well past the window, must still resolve as open"


def test_an_unreadable_on_block_is_not_softened_by_a_young_commit() -> None:
    """Age says nothing about whether a workflow file could be parsed."""
    lines = branch.undispatched_lines([_UNREADABLE_WF], age_secs=1,
                                      grace=branch._GRACE)
    line = _loud_line(lines, "mystery")
    assert "UNKNOWN" in line, line
    assert "still expected" not in line, line
