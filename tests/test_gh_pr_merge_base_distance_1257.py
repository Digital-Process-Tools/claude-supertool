"""The merge preview says how far the base has moved since the checks ran (#1257).

`master` at `664a26b` failed 13 of 15 legs from two PRs that were each 22/22
green four minutes apart: #1238 added a preset op with no `safety` key, #1243
added the whole-tree test that requires one. Disjoint files, so nothing
conflict-shaped fired, and `gh-pr-merge` read 22 legs, summed them correctly
and reported CLEAN. **The tally is a statement about the PR's merge-base** and
the op never asked whether that base was still the default branch head.

The disclosure is three-state per `docs/validators.md`: behind by N, level with
the base, or UNKNOWN. It never blocks — see
`test_a_stale_base_discloses_and_still_merges`, which is the pin on that call.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

MOD_PATH = Path(__file__).parent.parent / "presets" / "github" / "pr_merge.py"
_spec = importlib.util.spec_from_file_location("github_pr_merge_base", MOD_PATH)
assert _spec is not None and _spec.loader is not None
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


def _sibling():
    spec = importlib.util.spec_from_file_location(
        "gh_pr_merge_main_for_1257",
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


def test_a_base_that_has_not_moved_is_stated_not_omitted(monkeypatch, capsys):
    """The level case gets a line too.

    An absent warning and an unasked question render identically, which is the
    whole complaint — so `behind_by == 0` is printed rather than skipped.
    """
    h, rc, out = _run(monkeypatch, capsys, behind_by=0)
    assert "## Base" in out
    assert "has not moved" in out
    assert h.compare_calls, "the base distance was never asked for"


def test_a_stale_base_names_the_count_and_says_what_the_tally_covers(
        monkeypatch, capsys):
    h, rc, out = _run(monkeypatch, capsys, behind_by=3)
    assert "## Base" in out
    assert "3 commits" in out
    assert "BEHIND" in out


def test_a_stale_base_discloses_and_still_merges(monkeypatch, capsys):
    """The judgment call, pinned.

    Disclose-only. Blocking on `behind by N` would make a busy afternoon serial
    and take back what `changelog.d` fragments bought — four merges in one
    afternoon on 2026-08-07 with zero rebases. If this ever becomes a refusal
    it must be a deliberate change to this assertion, not a drift.
    """
    h, rc, out = _run(monkeypatch, capsys, behind_by=12)
    assert h.merge_calls, "a stale base blocked the merge; it must only disclose"
    assert rc == 0


def test_an_unreadable_base_distance_is_unknown_not_silence(monkeypatch, capsys):
    """The third state.

    A compare call that did not answer must not render as a base that has not
    moved — that is the absence-produced-by-the-tool read as an absence in the
    world, one layer up from the tally itself.
    """
    h, rc, out = _run(monkeypatch, capsys, fail_json={"compare"})
    assert "## Base" in out
    assert "UNKNOWN" in out
    assert "has not moved" not in out
    assert h.merge_calls, "an unreadable distance blocked the merge"


def test_the_base_line_is_printed_on_a_refused_preview_too(monkeypatch, capsys):
    """The preview is where this is read.

    Without `force` the op previews and merges nothing; a disclosure that only
    appears downstream of the gate is absent from every run a human actually
    reads before deciding.
    """
    h, rc, out = _run(monkeypatch, capsys, behind_by=4,
                      over={"statusCheckRollup": [_sib._leg("e2e", "FAILURE")]},
                      argv=["944"])
    assert h.merge_calls == []
    assert "## Base" in out
    assert "4 commits" in out


def test_the_compare_is_asked_about_the_commit_the_checks_ran_on(
        monkeypatch, capsys):
    """`headRefOid`, never `headRefName`.

    The tally belongs to a commit. A ref name resolves to whatever the branch
    points at now, which is a different commit the moment anybody pushes — and
    the answer would then be about a tree no check ever ran on.
    """
    h, rc, out = _run(monkeypatch, capsys, behind_by=1)
    path = [a for a in h.compare_calls[0] if "compare/" in a][0]
    assert "master..." + "c" * 40 in path


def test_base_distance_lines_declines_rather_than_guessing_on_a_bad_payload():
    """A non-integer `behind_by` is not zero."""
    lines = m.base_distance_lines("master", "c" * 40, None, "", "",
                                  "gh returned invalid JSON")
    assert any("UNKNOWN" in ln for ln in lines)
    assert not any("has not moved" in ln for ln in lines)
