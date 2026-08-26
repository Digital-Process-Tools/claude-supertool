"""`gh-pr-merge`'s receipt says nothing about what is left unwatched (#1851).

A stacked follow-up — a second open PR whose base is the branch just merged —
is knowable only by the merging op itself, because it is a question about the
board *after* this merge changed it. `stacked_followups()` answers it in three
states, matching every other disclosure in this file: found (named), none, or
UNKNOWN when the read itself failed. `STACK_NONE` must never stand in for a
read that never happened — the same shape #1947 fixes one function over.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

MOD_PATH = Path(__file__).parent.parent / "presets" / "github" / "pr_merge.py"
_spec = importlib.util.spec_from_file_location(
    "gh_pr_merge_stacked_followup_1851", MOD_PATH)
assert _spec is not None and _spec.loader is not None
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


def _sibling():
    spec = importlib.util.spec_from_file_location(
        "gh_pr_merge_main_for_1851",
        Path(__file__).parent / "test_gh_pr_merge_main_950.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_sib = _sibling()


def _run(monkeypatch, capsys, **kw):
    argv = kw.pop("argv", ["944", "squash", "force"])
    h = _sib._Harness(_sib._pr(**kw.pop("over", {})), **kw)
    monkeypatch.setattr(m, "_gh", h.gh)
    monkeypatch.setattr(m, "_gh_json", h.gh_json)
    monkeypatch.setattr(m, "_load_pr_module", h.pr_module)
    monkeypatch.setattr(m.subprocess, "run", h.subprocess_run)
    monkeypatch.setattr(sys, "argv", ["pr_merge.py"] + argv)
    monkeypatch.setenv("SUPERTOOL_NO_PUBLISH_CONFIRM", "1")
    rc = m.main()
    return h, rc, capsys.readouterr().out


# ---------------------------------------------------------------------------
# `stacked_followups` in isolation
# ---------------------------------------------------------------------------

def test_no_open_pr_targets_the_branch_is_none_not_silence(monkeypatch) -> None:
    monkeypatch.setattr(m, "_gh_json", lambda args, timeout=30: ([], ""))
    state, lines = m.stacked_followups("fix/924")
    assert state == m.STACK_NONE
    assert any("none" in ln for ln in lines)


def test_an_open_pr_targeting_the_branch_is_named(monkeypatch) -> None:
    monkeypatch.setattr(
        m, "_gh_json",
        lambda args, timeout=30: (
            [{"number": 1852, "title": "heal the fleet",
              "url": "https://github.com/o/r/pull/1852"}], ""))
    state, lines = m.stacked_followups("fix/924")
    assert state == m.STACK_FOUND
    text = "\n".join(lines)
    assert "1852" in text and "heal the fleet" in text


def test_a_failed_read_is_unknown_not_none(monkeypatch) -> None:
    """The paired negative: a harness that answers nothing at all must not
    pass as the ordinary 'no follow-up' case."""
    monkeypatch.setattr(m, "_gh_json",
                         lambda args, timeout=30: (None, "gh timed out"))
    state, lines = m.stacked_followups("fix/924")
    assert state == m.UNKNOWN
    assert state != m.STACK_NONE
    assert any("gh timed out" in ln for ln in lines)


def test_an_extraordinary_head_name_is_refused_not_searched_for(
        monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(m, "_gh_json",
                         lambda args, timeout=30: (calls.append(args), ([], ""))[1])
    state, lines = m.stacked_followups("-D;rm -rf /")
    assert state == m.UNKNOWN
    assert calls == [], "an extraordinary name must never reach gh pr list"


# ---------------------------------------------------------------------------
# wired into `main()`
# ---------------------------------------------------------------------------

def test_the_receipt_carries_a_stacked_follow_up_section(monkeypatch, capsys):
    h, rc, out = _run(monkeypatch, capsys, stack_prs=[])
    assert "## Stacked follow-up" in out
    assert h.stack_calls, "the merged branch was never asked about"
    assert "none" in out


def test_a_named_follow_up_reaches_the_printed_receipt(monkeypatch, capsys):
    h, rc, out = _run(monkeypatch, capsys,
                      stack_prs=[{"number": 1852, "title": "heal the fleet",
                                 "url": "https://github.com/o/r/pull/1852"}])
    assert "## Stacked follow-up" in out
    assert "1852" in out
    assert "Stacked follow-up: found." in out


def test_an_unreadable_stack_search_is_unknown_in_the_result_line(
        monkeypatch, capsys):
    h, rc, out = _run(monkeypatch, capsys, fail_json={"stack"})
    assert "## Stacked follow-up" in out
    assert f"Stacked follow-up: {m.UNKNOWN}." in out


def test_an_unconfirmed_merge_skips_the_stack_search_entirely(
        monkeypatch, capsys):
    h, rc, out = _run(monkeypatch, capsys, merge_rc=1,
                      after={"state": "OPEN", "mergedAt": None,
                             "mergeCommit": None})
    assert "## Stacked follow-up" in out
    assert "Skipped" in out
    assert not h.stack_calls, "an unconfirmed merge must not search the board"


def test_a_changelog_fragment_exists() -> None:
    from _changelog_findable import assert_change_is_findable
    assert_change_is_findable(1851)
