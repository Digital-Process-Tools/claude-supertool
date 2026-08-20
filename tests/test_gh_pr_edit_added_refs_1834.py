"""The none -> some transition on `gh-pr-edit`'s closing-reference receipt (#1834, #1788).

`closing_ref_verdict` reported on the **pre-edit** reference set and then
described the post-edit body with it. An edit that added the first `Closes` line
to a published body that had none rendered, in one block:

    ## Closing references
      the published body linked no issue, and neither does this one
      Issue: #321

The first line is false; the second is right and GitHub binds the reference
(claude-oss #268, #273, #277 in #1788; #331, #332 in #1834 — the last adding
two, so it is not specific to a single reference).

The gate itself was never wrong: nothing was lost, so `REF_OK` and the write are
correct. Only the sentence was. So every assertion here is on the **message**,
which is the half nothing in `test_gh_pr_edit_1739.py` looks at — its
`test_a_reference_only_the_new_body_has_is_not_a_loss` covers exactly this input
and asserts only `state` and `lost`, both of which were already right.

The last three tests are #1835, which is the same defect one op over: text that
overstates what the tool does.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
MOD_PATH = _ROOT / "presets" / "github" / "pr_edit.py"
_spec = importlib.util.spec_from_file_location("github_pr_edit_1834", MOD_PATH)
assert _spec is not None and _spec.loader is not None
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

# `pr_edit` puts `presets/` on `sys.path` when it loads, so this is its reader,
# not a second copy of one.
import _checks  # noqa: E402

_GH_OPS = json.loads(
    (_ROOT / "presets" / "github.json").read_text(encoding="utf-8"))["ops"]


# ===========================================================================
# the transition that had no arm — none -> some
# ===========================================================================

def test_a_reference_the_edit_adds_is_reported_as_added():
    state, lost, msg = m.closing_ref_verdict(
        "prose, and no closing keyword anywhere", "Closes #321.\n\nprose", "")
    assert state == m.REF_OK
    assert lost == []
    assert "added: #321" in msg
    assert "neither does this one" not in msg


def test_two_references_the_edit_adds_are_both_named():
    """claude-oss #332 added two at once, which rules out a single-ref cause."""
    _state, _lost, msg = m.closing_ref_verdict(
        "prose", "Closes #275, closes #296.", "")
    assert "added: #275, #296" in msg


def test_a_backticked_keyword_in_the_published_body_is_still_an_addition():
    """The exact #332 body: the pre-edit parse correctly saw nothing, because a
    code span binds nothing. Both halves of the reader run in this one call and
    only the post-edit half used to misreport."""
    old = "prose\n\n`Closes #275, closes #296`\n"
    _state, _lost, msg = m.closing_ref_verdict(old, "Closes #275.", "")
    assert "added: #275" in msg


def test_an_addition_alongside_a_reference_that_survived_names_both_halves():
    """`carried through` alone named only the old set, so a second `Closes`
    added to a body that already had one went unmentioned."""
    _state, _lost, msg = m.closing_ref_verdict(
        "Closes #230.", "Closes #230.\n\nCloses #231.", "")
    assert "carried through: #230" in msg
    assert "added: #231" in msg


# ===========================================================================
# the positive controls — the arms that were already right must not move
# ===========================================================================

def test_a_reference_that_survived_untouched_still_reads_carried_through():
    _state, _lost, msg = m.closing_ref_verdict(
        "Closes #1739.\n\nold prose", "Closes #1739.\n\nnew prose", "")
    assert msg == "carried through: #1739"
    assert "added" not in msg


def test_a_body_that_genuinely_links_nothing_still_says_so():
    _state, _lost, msg = m.closing_ref_verdict(
        "no reference here", "still none", "")
    assert msg == "the published body linked no issue, and neither does this one"
    assert "added" not in msg


def test_a_dropped_reference_still_refuses_and_is_not_reworded_as_an_addition():
    """`some -> none` is the DROPPED arm and this change must not reach it."""
    state, lost, msg = m.closing_ref_verdict(
        "Closes #1739.", "prose with no reference", "")
    assert state == m.REF_DROPPED
    assert lost == ["#1739"]
    assert "DROPS #1739" in msg
    assert m.may_write(state, unlink=False) is False


def test_an_unreadable_published_body_is_still_unknown_not_an_addition():
    """The one input where the new set is non-empty and `added` would be a lie:
    what the old body linked was never established."""
    state, _lost, msg = m.closing_ref_verdict(None, "Closes #321.", "gh timed out")
    assert state == m.REF_UNKNOWN
    assert "added" not in msg
    assert "UNKNOWN" in msg


# ===========================================================================
# the invariant the receipt broke — the two lines cannot contradict each other
# ===========================================================================

def test_carried_through_is_ordered_by_the_body_being_written():
    """A body that only reorders its own `Closes` lines. Both lines were true
    before — the sets agree — but they listed the same two references in
    opposite orders, so agreeing meant reading them as sets. Found by the
    review of this change's own first commit."""
    _state, _lost, msg = m.closing_ref_verdict(
        "Closes #1, closes #2.", "Closes #2, closes #1.", "")
    assert msg == "carried through: #2, #1"
    assert _checks.linked_issue_line(
        _checks.closing_issue_refs("Closes #2, closes #1.")) == "Issues: #2, #1"


def test_a_cross_repo_reference_is_not_confused_with_a_local_one():
    """`closing_issue_refs` returns display forms, so `owner/repo#5` and `#5`
    are different strings and the set arithmetic must keep them apart."""
    _state, _lost, msg = m.closing_ref_verdict(
        "Closes o/r#5.", "Closes o/r#5.\n\nCloses #5.", "")
    assert msg == "carried through: o/r#5; added: #5"


@pytest.mark.parametrize("old,new", [
    ("prose", "Closes #321."),
    ("prose", "Closes #275, closes #296."),
    ("`Closes #275, closes #296`", "Closes #275, closes #296."),
    ("Closes #230.", "Closes #230.\n\nCloses #231."),
    ("Closes #1739.", "Closes #1739."),
    ("Closes #1, closes #2.", "Closes #2, closes #1."),
    ("Closes o/r#5.", "Closes o/r#5.\n\nCloses #5."),
    ("prose", "more prose"),
])
def test_the_two_receipt_lines_never_contradict_each_other(old, new):
    """`main` prints `ref_msg` and then `linked_issue_line(new refs)` beneath it.
    Whenever the second names an issue, the first must name it too and must not
    claim the body links none. That is the bug as filed, stated as a property."""
    state, _lost, msg = m.closing_ref_verdict(old, new, "")
    assert state == m.REF_OK, (old, new)
    refs = _checks.closing_issue_refs(new)
    issue_line = _checks.linked_issue_line(refs)
    if refs:
        assert "linked no issue, and neither does this one" not in msg, (
            f"receipt contradicts itself: {msg!r} printed above {issue_line!r}")
        for ref in refs:
            assert ref in msg, (
                f"{ref} is bound by the new body and unnamed in {msg!r}")
    else:
        assert "added" not in msg


# ===========================================================================
# #1835 — one word in gh-pr-create's help text
# ===========================================================================

def test_gh_pr_create_does_not_claim_to_catch_a_malformed_closing_line():
    """It reports and then opens the pull request, exit 0. `caught` reads as
    *prevented*, and that reading propagated downstream into a consuming repo's
    skill as `refuses`, which is false and made the receipt — the only place the
    problem is visible — look skippable."""
    desc = _GH_OPS["gh-pr-create"]["description"]
    assert "caught at creation" not in desc
    assert "reported at creation" in desc


def test_gh_pr_create_says_it_does_not_refuse_and_names_the_repair():
    desc = _GH_OPS["gh-pr-create"]["description"]
    assert "NOT refused" in desc
    assert "gh-pr-edit" in desc, "the op that adds the line afterwards"


def test_gh_pr_edit_still_documents_the_refusal_it_actually_makes():
    """The positive control for the two above: `refuse` is correct about
    `gh-pr-edit`, so the fix cannot be `delete the word everywhere`."""
    desc = _GH_OPS["gh-pr-edit"]["description"]
    assert "unlink" in desc
    assert "REFUSE" in desc.upper()
