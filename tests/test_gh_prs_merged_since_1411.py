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

_spec = importlib.util.spec_from_file_location(
    "github_since_tag_1411", PRESETS / "github" / "since_tag.py")
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


def test_a_value_that_is_not_an_instant_is_refused_not_dropped() -> None:
    """#939's class: an unmapped value builds the argv without the flag, and the
    unbounded board renders as the answer to a bounded question."""
    bad = prs._bad_values({"merged-since": "friday"})
    assert [k for k, _v, _e in bad] == ["merged-since"]


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

def test_since_tag_no_longer_names_gh_pr_list_anywhere() -> None:
    """The direct pin on the defect. `since_tag` may not know the argv's shape."""
    source = (PRESETS / "github" / "since_tag.py").read_text(encoding="utf-8")
    assert '"pr", "list"' not in source
    assert '"gh", "pr"' not in source


def test_both_callers_produce_the_same_argv_for_the_same_boundary() -> None:
    """The refactor's guarantee, stated as an equality rather than as "it still
    works". Only one of these two expressions may know how to build the argv."""
    seen = {}

    def _capture(argv, timeout):
        seen["argv"] = argv
        return True, "[]", ""

    original = st._run
    st._run = _capture
    try:
        st.read_merged_prs(BOUNDARY, 100)
    finally:
        st._run = original

    assert seen["argv"] == prs._build_list_cmd(
        {"state": "merged", "merged-since": STAMP}, 100,
        fields=st.PR_LIST_FIELDS)


def test_since_tag_delegates_rather_than_reproducing_the_argv() -> None:
    """The equality above would also hold if `since_tag` rebuilt an identical
    list by hand, which is the state this issue is about. This one goes red on
    that, because a sentinel returned by the shared builder must reach `_run`."""
    sentinel = ["gh", "pr", "list", "--sentinel"]
    seen = {}

    def _capture(argv, timeout):
        seen["argv"] = argv
        return True, "[]", ""

    original_run, original_build = st._run, prs._build_list_cmd
    st._run = _capture
    prs._build_list_cmd = lambda *a, **k: list(sentinel)
    try:
        st.read_merged_prs(BOUNDARY, 100)
    finally:
        st._run, prs._build_list_cmd = original_run, original_build

    assert seen["argv"] == sentinel


def test_since_tag_does_not_pay_for_the_boards_field_set() -> None:
    """`_LIST_FIELDS` carries `statusCheckRollup` — dozens of check runs per PR,
    over a page of 100 merged PRs. The shared thing is the query, not the
    payload, so the field set is the caller's."""
    assert "statusCheckRollup" not in st.PR_LIST_FIELDS
    assert "mergedAt" in st.PR_LIST_FIELDS


# ---------------------------------------------------------------------------
# What must NOT have moved
# ---------------------------------------------------------------------------

def test_gh_prs_did_not_acquire_the_changelog_read_or_the_repo_refusal() -> None:
    """Two renders over one source of truth are normal; the judgement is not
    duplicated and it is not relocated either. `changelog.d` is a local
    filesystem read and the `repo:` refusal exists because only the PR list can
    follow a target — both stay in `since_tag`, whose own tests cover them."""
    source = (PRESETS / "github" / "prs.py").read_text(encoding="utf-8")
    assert "changelog" not in source.lower()
    assert not hasattr(prs, "repo_target_refusal")
    assert hasattr(st, "repo_target_refusal")


def test_the_boundary_states_stay_where_the_three_of_them_are() -> None:
    """A listing filter has two outcomes — apply the token or refuse it. Tag
    resolution has three, and `gh-prs` is `repo_target: true` while a tag is a
    read of the local clone, so a tag name accepted here would resolve against
    one repository and list from another. Instants only."""
    assert prs._bad_values({"merged-since": "v0.31.0"})


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
