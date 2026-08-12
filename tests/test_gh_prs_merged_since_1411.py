"""`gh-prs:merged-since=` and the third caller it deletes (#1411).

`presets/github/since_tag.py` hand-built its own `gh pr list` argv. That made it
a **third** caller of the endpoint `presets/github/prs.py:_build_list_cmd` owns,
and that function's docstring opens "No role filter is ever added here" only
because the listing drifted twice already — #1207 put a default `author=@me`
into it, #1230 then found `radar`'s tier had inherited the narrow board after
the op dropped it. Two ops and one board off one listing.

Nothing was wrong with the copy. The defect is that a second place exists where
the same thing can go wrong, in a file whose history is exactly that.

So `gh-prs` grows the boundary vocabulary it was missing — there is no op today
that answers "what merged since Friday" — and `since_tag` sheds its argv.

The load-bearing tests here are the ones that pin **both callers against one
argv**. A test that only asserts `gh-since-tag` still works would pass if the
duplication were still there, which is the whole thing being removed.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

PRESETS = Path(__file__).parent.parent / "presets"
sys.path.insert(0, str(PRESETS))
sys.path.insert(0, str(PRESETS / "github"))

import _filter_tokens  # noqa: E402
import prs  # noqa: E402

# #1405 folded `gh-since-tag` into this filter and retired the op. `since_tag.py`
# is a tombstone; the module these tests are about is `_release_gate.py`, which
# is the same file under a new name. TWO ASSERTIONS BELOW ARE DELIBERATELY
# REVERSED by that fold and say so where they sit — they were right for the
# shape #1411 left behind and they are wrong for this one.
_spec = importlib.util.spec_from_file_location(
    "github_since_tag_1411", PRESETS / "github" / "_release_gate.py")
assert _spec is not None and _spec.loader is not None
st = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(st)


# The instant from #1209's own report, so the two issues' tests agree on it.
BOUNDARY = datetime(2026, 8, 9, 15, 13, 43, tzinfo=timezone.utc)
STAMP = "2026-08-09T15:13:43+00:00"


# ---------------------------------------------------------------------------
# The filter exists at all
# ---------------------------------------------------------------------------

def test_merged_since_is_a_filter_key_not_an_unrecognised_token() -> None:
    """Absent from `_FILTER_KEYS`, the token is refused and no boundary exists."""
    assert "merged-since" in prs._FILTER_KEYS


def test_merged_since_reaches_gh_as_a_search_qualifier() -> None:
    cmd = prs._build_list_cmd({"state": "merged", "merged-since": STAMP}, 50)
    assert "--search" in cmd
    assert cmd[cmd.index("--search") + 1] == "merged:>" + STAMP


def test_a_bare_date_is_accepted_and_normalised_to_an_instant() -> None:
    """The only spelling the `:` tokenizer lets a caller type — see below."""
    cmd = prs._build_list_cmd({"state": "merged", "merged-since": "2026-08-09"}, 50)
    assert cmd[cmd.index("--search") + 1] == "merged:>2026-08-09T00:00:00+00:00"


def test_a_value_that_is_neither_a_date_nor_a_ref_is_refused_not_dropped() -> None:
    """#939's class: an unmapped value builds the argv without the flag, and the
    unbounded board renders as the answer to a bounded question.

    Since #1405 the domain is "a date OR a tag name", so the value that proves
    this has to be neither. A space is the cheapest way to be neither: no date
    parses it and no git ref may contain one.
    """
    bad = prs._bad_values({"merged-since": "last friday"})
    assert [k for k, _v, _e in bad] == ["merged-since"]


def test_a_tag_shaped_value_that_does_not_resolve_still_builds_no_argv() -> None:
    """The same guarantee one layer along, for the values #1405 started accepting.

    `friday` is now shape-legal — it could be a tag name — so it passes
    `_bad_values` and is caught at resolution instead. What may never happen is
    the #939 shape: a board built without the boundary. `resolve_boundary`
    returning anything but RESOLVED carries an empty `stamp`, and `main_with_args`
    returns before the argv is built.
    """
    assert prs._bad_values({"merged-since": "friday"}) == []
    original = st._run
    st._run = lambda argv, timeout: (True, "", "")   # no tags exist
    try:
        boundary = st.resolve_boundary("friday")
    finally:
        st._run = original
    assert boundary.state != st.BOUNDARY_RESOLVED
    assert boundary.stamp == ""
    assert boundary.refusal


# ---------------------------------------------------------------------------
# The boundary is refused where it could only ever return an empty board
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("state", ["merged", "closed", "all"])
def test_merged_since_is_allowed_beside_a_state_that_can_contain_merges(state) -> None:
    assert prs._state_conflict({"state": state, "merged-since": STAMP}) is None


@pytest.mark.parametrize("filters", [
    {"merged-since": STAMP},
    {"state": "open", "merged-since": STAMP},
])
def test_merged_since_on_an_open_board_is_refused_rather_than_answered(filters) -> None:
    """`--search merged:>X` over open PRs matches nothing, ever. Printing the
    empty board would be the strongest available statement of absence made about
    a query that could not have returned a row — the tool's own absence read as
    the world's."""
    msg = prs._state_conflict(filters)
    assert msg is not None
    assert "state=merged" in msg


# ---------------------------------------------------------------------------
# One `--search`, not two
# ---------------------------------------------------------------------------

def test_two_search_qualifiers_compose_into_one_flag() -> None:
    """`reviewer=` already routed through `--search`. A second `--search` on the
    same argv is not a second filter: gh binds one string, so the later flag
    replaces the earlier one and the board silently answers the other half of
    the question."""
    cmd = prs._build_list_cmd(
        {"state": "merged", "reviewer": "@me", "merged-since": STAMP}, 50)
    assert cmd.count("--search") == 1
    qualifier = cmd[cmd.index("--search") + 1]
    assert "review-requested:@me" in qualifier
    assert "merged:>" + STAMP in qualifier


# ---------------------------------------------------------------------------
# The duplication itself — both callers, one argv
# ---------------------------------------------------------------------------

def test_the_gate_no_longer_names_gh_pr_list_anywhere() -> None:
    """The direct pin on the defect. The gate may not know the argv's shape.

    Stronger after #1405 than before it: the gate does not fetch at all now.
    `gh-prs` makes the one call and hands the rows in.
    """
    source = (PRESETS / "github" / "_release_gate.py").read_text(encoding="utf-8")
    assert '"pr", "list"' not in source
    assert '"gh", "pr"' not in source


def test_there_is_only_one_caller_left_to_disagree_with() -> None:
    """#1411's equality became an identity: the second caller is gone.

    The two tests here that captured `since_tag.read_merged_prs`'s argv and
    compared it to `_build_list_cmd`'s cannot be written any more, because the
    function they drove no longer exists — `gh-prs` makes the call. That is the
    duplication removed rather than pinned, so what is left to assert is that
    nothing grew back.
    """
    assert not hasattr(st, "read_merged_prs")
    source = (PRESETS / "github" / "_release_gate.py").read_text(encoding="utf-8")
    assert "_build_list_cmd" not in source


def test_the_boundary_slice_does_not_pay_for_the_boards_field_set() -> None:
    """`_LIST_FIELDS` carries `statusCheckRollup` — dozens of check runs per PR,
    over a page of up to 500 merged ones. #1411 kept the gate off that field
    set and #1405 kept it off: the boundary slice fetches its own narrow set,
    which is why `failed` beside `merged-since=` is refused rather than
    answered from a field nobody requested."""
    assert "statusCheckRollup" not in st.PR_LIST_FIELDS
    assert "mergedAt" in st.PR_LIST_FIELDS
    assert "statusCheckRollup" in prs._LIST_FIELDS
    assert prs._boundary_flag_conflict(
        prs._gate_plan({"merged-since": "v0.34.0"}), {"failed"})


# ---------------------------------------------------------------------------
# What must NOT have moved
# ---------------------------------------------------------------------------

def test_gh_prs_DID_acquire_the_changelog_read_and_the_repo_refusal() -> None:
    """REVERSED by #1405, deliberately. This test asserted the opposite.

    #1411's reasoning was that `changelog.d` is a local filesystem read and the
    `repo:` refusal exists because only the PR list can follow a target, so
    neither belonged on `gh-prs`. Both are still true as statements about the
    code. What changed is the decision: the fold was chosen, and the honest
    accounting is that these two did not become unnecessary — they **relocated
    onto the op with the most callers**, which is a cost the fold pays and not
    a problem it solved.

    So the guard moved rather than disappearing, and it is narrower than the
    old one: `repo_target_refusal` now fires only for `merged-since=<tag>`,
    because a date boundary reads nothing local and follows a target whole.
    """
    assert hasattr(prs, "_tag_target_conflict")
    assert prs._tag_target_conflict("v0.34.0", "owner/name")
    assert prs._tag_target_conflict("2026-08-09", "owner/name") is None
    assert prs._tag_target_conflict("v0.34.0", "") is None
    # Relocated, but reached through the gate rather than reimplemented: the
    # fragment count and the boundary states have one home, and this file is
    # not it. (A bare `changelog.d` substring will not do — prs.py names it in
    # prose, describing what the ordinary board does NOT read.)
    source = (PRESETS / "github" / "prs.py").read_text(encoding="utf-8")
    assert "count_fragments" not in source
    assert "_release_gate" in source


def test_the_boundary_states_reach_the_filter_as_refusals() -> None:
    """REVERSED by #1405, deliberately. This test asserted `v0.31.0` was refused.

    #1411's reasoning: a listing filter has two outcomes and tag resolution has
    three, so a tag name may not be a filter value. The third state is real and
    it did not go away — what the fold established is that a filter can carry
    three outcomes as long as the extra one is a **refusal**. RESOLVED applies;
    AMBIGUOUS and UNRESOLVED print no board at all. What a filter may never do
    is pick between two defensible boundaries, and nothing here does.

    The other half of #1411's reasoning — `gh-prs` follows `repo:` while a tag
    is a local read — was correct and survives as `_tag_target_conflict` above.
    """
    assert prs._bad_values({"merged-since": "v0.31.0"}) == []
    plan = prs._gate_plan({"merged-since": "v0.31.0"})
    assert plan is not None and plan.is_tag is True


# ---------------------------------------------------------------------------
# One clock, shared — #1209's own lesson applied to the filter
# ---------------------------------------------------------------------------

def test_the_filter_and_the_row_check_parse_with_the_same_function() -> None:
    """#1209 was two clocks and one comparison. A private instant parser beside
    `since_tag`'s would be that shape again."""
    both = ("2026-08-09T16:07:45Z", "2026-08-09T17:13:43+02:00", "2026-08-09")
    for text in both:
        assert st.parse_instant(text) == _filter_tokens.parse_iso_instant(text)
    assert st.parse_instant("nonsense") is None
    assert _filter_tokens.parse_iso_instant("nonsense") is None


# ---------------------------------------------------------------------------
# Adjacent: the scope note hardcoded "open"
# ---------------------------------------------------------------------------

def test_the_unfiltered_scope_note_names_the_state_it_is_about() -> None:
    """It said "every author's **open** PRs" whatever `state=` asked for. Under
    `state=merged` that labels a merged board as the open one — a wrong label on
    the line whose whole job is saying which population is on screen."""
    note = prs._scope_note({"state": "merged", "merged-since": STAMP}, 50, 3)
    assert note is not None
    assert "open" not in note


def test_a_timestamp_refusal_names_the_spelling_that_works() -> None:
    """supertool splits an op argument on ':', so `merged-since=2026-08-09T16:07:45Z`
    arrives as three argv entries and the value is gone before any filter is
    parsed. The generic refusal ends "query it with the backend CLI directly and
    file the gap", which is now wrong advice — the date form works."""
    msg = prs._extra_segments_error(
        ["prs.py", "merged-since=2026-08-09T16", "07", "45Z"])
    assert msg is not None
    assert "merged-since=YYYY-MM-DD" in msg
