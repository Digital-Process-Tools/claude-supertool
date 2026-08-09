"""`gh-since-tag` — the release trigger's two numbers, and the zero that lied (#1209).

The op exists because the maintainer hand-rolled this every tick and one night it
printed `merged since tag: 0` beside `7` unreleased fragments. The cause was a
**string** comparison between `2026-08-09T16:07:45Z` (what `gh` returns) and
`2026-08-09T17:13:43+02:00` (what `git show -s --format=%cI` returns): `"16" > "17"`
is False at the second character, so every PR merged after the tag was filtered out
as being before it. The correct answer was 6.

Every test here is about a number that must not render as a confident zero.
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

PRESET_PATH = Path(__file__).parent.parent / "presets" / "github" / "since_tag.py"
_spec = importlib.util.spec_from_file_location("github_since_tag", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
st = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(st)


# ---------------------------------------------------------------------------
# parse_instant / filter_merged — the defect this issue was filed from
# ---------------------------------------------------------------------------

# The exact pair from #1209's report. Held as module constants so the two tests
# below cannot drift apart: one asserts the trap is real, the other that the op
# does not fall into it.
TAG_LOCAL = "2026-08-09T17:13:43+02:00"   # = 15:13:43Z
PR_AFTER_UTC = "2026-08-09T16:07:45Z"     # 53 minutes AFTER the tag
PR_BEFORE_UTC = "2026-08-09T14:00:00Z"    # 73 minutes BEFORE the tag


def test_the_string_comparison_really_does_get_it_backwards() -> None:
    """Not a test of our code — a pin on why the op may never compare strings."""
    assert (PR_AFTER_UTC > TAG_LOCAL) is False


def test_instants_are_compared_as_instants_not_as_strings() -> None:
    boundary = st.parse_instant(TAG_LOCAL)
    assert boundary is not None
    rows = [
        {"number": 1212, "title": "after", "mergedAt": PR_AFTER_UTC},
        {"number": 1100, "title": "before", "mergedAt": PR_BEFORE_UTC},
    ]
    kept, undated = st.filter_merged(rows, boundary)
    assert [r["number"] for r in kept] == [1212]
    assert undated == []


def test_parse_instant_normalises_both_forms_to_one_clock() -> None:
    assert st.parse_instant(TAG_LOCAL) == datetime(
        2026, 8, 9, 15, 13, 43, tzinfo=timezone.utc)
    assert st.parse_instant(PR_AFTER_UTC) == datetime(
        2026, 8, 9, 16, 7, 45, tzinfo=timezone.utc)


def test_the_tagged_commit_itself_is_not_counted_as_merged_since() -> None:
    """A PR merged AT the boundary is the tag, so it is in the release already."""
    boundary = st.parse_instant(TAG_LOCAL)
    kept, _ = st.filter_merged(
        [{"number": 9, "title": "the tag", "mergedAt": TAG_LOCAL}], boundary)
    assert kept == []


def test_an_unparsable_merged_at_is_reported_not_dropped() -> None:
    """A row the op could not place is not a row that is out of range."""
    boundary = st.parse_instant(TAG_LOCAL)
    rows = [
        {"number": 1212, "title": "after", "mergedAt": PR_AFTER_UTC},
        {"number": 1213, "title": "no date", "mergedAt": None},
        {"number": 1214, "title": "junk date", "mergedAt": "not-a-date"},
    ]
    kept, undated = st.filter_merged(rows, boundary)
    assert [r["number"] for r in kept] == [1212]
    assert sorted(r["number"] for r in undated) == [1213, 1214]


def test_kept_rows_come_back_in_merge_order() -> None:
    boundary = st.parse_instant(TAG_LOCAL)
    rows = [
        {"number": 3, "title": "c", "mergedAt": "2026-08-09T18:00:00Z"},
        {"number": 1, "title": "a", "mergedAt": "2026-08-09T16:00:00Z"},
        {"number": 2, "title": "b", "mergedAt": "2026-08-09T17:00:00Z"},
    ]
    kept, _ = st.filter_merged(rows, boundary)
    assert [r["number"] for r in kept] == [1, 2, 3]


# ---------------------------------------------------------------------------
# select_tag — what "the last tag" is, and when that question has no answer
# ---------------------------------------------------------------------------

def _tag(name, date, reachable=True, sha="abc1234", objtype="commit"):
    return {"name": name, "commit_date": date, "reachable": reachable,
            "sha": sha, "objtype": objtype}


def test_the_newest_version_tag_on_the_default_branch_wins() -> None:
    tags = [
        _tag("v0.30.0", "2026-08-09T08:29:26+02:00"),
        _tag("v0.31.0", "2026-08-09T17:13:43+02:00"),
        _tag("v0.29.0", "2026-08-08T19:48:36+02:00"),
    ]
    chosen, state, _notes = st.select_tag(tags, "")
    assert state == st.BOUNDARY_RESOLVED
    assert chosen["name"] == "v0.31.0"


def test_a_tag_without_a_leading_v_is_still_a_version_tag() -> None:
    """This repo carries both `v0.4.0` and `0.3.2`."""
    tags = [_tag("0.3.2", "2026-04-17T21:37:51+02:00")]
    chosen, state, _notes = st.select_tag(tags, "")
    assert state == st.BOUNDARY_RESOLVED
    assert chosen["name"] == "0.3.2"


def test_no_tags_at_all_is_unresolved_never_a_zero() -> None:
    chosen, state, notes = st.select_tag([], "")
    assert chosen is None
    assert state == st.BOUNDARY_UNRESOLVED
    assert notes and any("no tag" in n.lower() for n in notes)


def test_only_non_version_tags_is_unresolved_and_names_them() -> None:
    chosen, state, notes = st.select_tag([_tag("wip-241", "2026-05-29T15:43:21+02:00")], "")
    assert chosen is None
    assert state == st.BOUNDARY_UNRESOLVED
    assert any("wip-241" in n for n in notes)


def test_a_newer_non_version_tag_is_disclosed_but_does_not_make_it_ambiguous() -> None:
    """`wip-foo` is not a release boundary candidate, so it cannot rival one."""
    tags = [
        _tag("v0.31.0", "2026-08-09T17:13:43+02:00"),
        _tag("wip-241", "2026-08-09T19:00:00+02:00"),
    ]
    chosen, state, notes = st.select_tag(tags, "")
    assert chosen["name"] == "v0.31.0"
    assert state == st.BOUNDARY_RESOLVED
    assert any("wip-241" in n for n in notes)


def test_a_newer_version_tag_off_the_default_branch_is_ambiguous() -> None:
    """A release cut from a branch. Two defensible boundaries, so pick neither silently."""
    tags = [
        _tag("v0.31.0", "2026-08-09T17:13:43+02:00"),
        _tag("v0.31.1", "2026-08-09T20:00:00+02:00", reachable=False),
    ]
    chosen, state, notes = st.select_tag(tags, "")
    assert state == st.BOUNDARY_AMBIGUOUS
    assert chosen["name"] == "v0.31.0"
    assert any("v0.31.1" in n for n in notes)


def test_an_older_version_tag_off_the_default_branch_is_not_ambiguous() -> None:
    tags = [
        _tag("v0.31.0", "2026-08-09T17:13:43+02:00"),
        _tag("v0.30.9", "2026-08-08T20:00:00+02:00", reachable=False),
    ]
    _chosen, state, _notes = st.select_tag(tags, "")
    assert state == st.BOUNDARY_RESOLVED


def test_two_version_tags_at_the_same_instant_on_different_commits_is_ambiguous() -> None:
    tags = [
        _tag("v0.31.0", "2026-08-09T17:13:43+02:00", sha="aaa1111"),
        _tag("v1.0.0", "2026-08-09T17:13:43+02:00", sha="bbb2222"),
    ]
    _chosen, state, notes = st.select_tag(tags, "")
    assert state == st.BOUNDARY_AMBIGUOUS
    assert any("v1.0.0" in n for n in notes)


def test_two_tags_on_the_same_commit_are_one_boundary() -> None:
    tags = [
        _tag("v0.31.0", "2026-08-09T17:13:43+02:00", sha="aaa1111"),
        _tag("v0.31.0-final", "2026-08-09T17:13:43+02:00", sha="aaa1111"),
    ]
    _chosen, state, _notes = st.select_tag(tags, "")
    assert state == st.BOUNDARY_RESOLVED


def test_an_explicit_tag_overrides_every_selection_rule() -> None:
    tags = [
        _tag("v0.31.0", "2026-08-09T17:13:43+02:00"),
        _tag("wip-241", "2026-05-29T15:43:21+02:00"),
    ]
    chosen, state, _notes = st.select_tag(tags, "wip-241")
    assert state == st.BOUNDARY_RESOLVED
    assert chosen["name"] == "wip-241"


def test_an_explicit_tag_that_does_not_exist_is_unresolved_not_a_fallback() -> None:
    tags = [_tag("v0.31.0", "2026-08-09T17:13:43+02:00")]
    chosen, state, notes = st.select_tag(tags, "v9.9.9")
    assert chosen is None
    assert state == st.BOUNDARY_UNRESOLVED
    assert any("v9.9.9" in n for n in notes)


def test_an_explicit_tag_off_the_default_branch_says_so() -> None:
    tags = [_tag("v0.31.1", "2026-08-09T20:00:00+02:00", reachable=False)]
    chosen, state, notes = st.select_tag(tags, "v0.31.1")
    assert state == st.BOUNDARY_RESOLVED
    assert chosen["name"] == "v0.31.1"
    assert any("not an ancestor" in n for n in notes)


def test_a_tag_whose_date_cannot_be_parsed_is_not_silently_ordered_last() -> None:
    tags = [
        _tag("v0.31.0", "2026-08-09T17:13:43+02:00"),
        _tag("v0.32.0", "garbage"),
    ]
    _chosen, state, notes = st.select_tag(tags, "")
    assert state == st.BOUNDARY_AMBIGUOUS
    assert any("v0.32.0" in n for n in notes)


# ---------------------------------------------------------------------------
# The count's own three states
# ---------------------------------------------------------------------------

def test_a_full_page_is_a_lower_bound_not_a_count() -> None:
    state, text = st.count_state(kept=5, limit=5, undated=0, unreconciled=0)
    assert state == st.COUNT_LOWER_BOUND
    assert text.startswith(">=")


def test_a_short_page_is_an_exact_count() -> None:
    state, text = st.count_state(kept=6, limit=100, undated=0, unreconciled=0)
    assert state == st.COUNT_EXACT
    assert text == "6"


def test_an_undated_row_makes_the_count_unverified() -> None:
    state, _text = st.count_state(kept=6, limit=100, undated=1, unreconciled=0)
    assert state == st.COUNT_UNVERIFIED


def test_a_reconciliation_gap_makes_the_count_unverified() -> None:
    state, _text = st.count_state(kept=6, limit=100, undated=0, unreconciled=2)
    assert state == st.COUNT_UNVERIFIED


# ---------------------------------------------------------------------------
# Fragments — the other number the gate reads
# ---------------------------------------------------------------------------

def test_fragments_are_counted_by_section_and_readme_is_not_one(tmp_path) -> None:
    d = tmp_path / "changelog.d"
    d.mkdir()
    for name in ("1157.fixed.md", "1180.security.md", "1181.fixed.md",
                 "1194.changed.md", "README.md"):
        (d / name).write_text("- x\n", encoding="utf-8")
    count, sections, note = st.count_fragments(str(d))
    assert count == 4
    assert sections == {"fixed": 2, "security": 1, "changed": 1}
    assert note == ""


def test_a_missing_changelog_dir_is_unknown_not_zero(tmp_path) -> None:
    count, sections, note = st.count_fragments(str(tmp_path / "nope"))
    assert count is None
    assert sections == {}
    assert note


def test_an_empty_changelog_dir_really_is_zero(tmp_path) -> None:
    d = tmp_path / "changelog.d"
    d.mkdir()
    count, _sections, note = st.count_fragments(str(d))
    assert count == 0
    assert note == ""


def test_a_fragment_with_an_unreadable_name_is_counted_under_an_unknown_section(tmp_path) -> None:
    d = tmp_path / "changelog.d"
    d.mkdir()
    (d / "1157.fixed.md").write_text("- x\n", encoding="utf-8")
    (d / "orphan.md").write_text("- x\n", encoding="utf-8")
    count, sections, _note = st.count_fragments(str(d))
    assert count == 2
    assert sections.get("?") == 1


# ---------------------------------------------------------------------------
# The second source of truth — local history against the API
# ---------------------------------------------------------------------------

def test_a_pr_number_is_read_off_a_squash_subject() -> None:
    nums, unattributed = st.numbers_from_subjects([
        "gh-prs defaults to the repo, the tally cap was the real cause (#1212)",
        "Wire this repo to claude-jit-context (#1214)",
    ])
    assert nums == {1212, 1214}
    assert unattributed == []


def test_a_subject_carrying_no_pr_number_is_reported_not_ignored() -> None:
    nums, unattributed = st.numbers_from_subjects([
        "Fix the thing (#1212)",
        "a direct push with no PR",
    ])
    assert nums == {1212}
    assert unattributed == ["a direct push with no PR"]


def test_a_number_mid_subject_is_not_mistaken_for_the_merge_reference() -> None:
    nums, unattributed = st.numbers_from_subjects(["Revert (#99) because it broke"])
    assert nums == set()
    assert unattributed == ["Revert (#99) because it broke"]


def test_reconcile_reports_both_directions() -> None:
    only_api, only_git = st.reconcile({1, 2, 3}, {2, 3, 4})
    assert only_api == [1]
    assert only_git == [4]


def test_reconcile_agreeing_is_empty_both_ways() -> None:
    assert st.reconcile({1, 2}, {2, 1}) == ([], [])


# ---------------------------------------------------------------------------
# Render — the whole point is that a zero never stands alone
# ---------------------------------------------------------------------------

def test_an_unresolved_boundary_never_renders_a_merged_count() -> None:
    out = "\n".join(st.render(
        boundary_state=st.BOUNDARY_UNRESOLVED,
        chosen=None,
        notes=["no tag on this repository matches a version shape"],
        rows=[], undated=[], count_text="?", count_state=st.COUNT_UNKNOWN,
        fragments=(7, {"fixed": 7}, ""),
        only_api=[], only_git=[], unattributed=[], sources=["default branch: master"],
    ))
    assert "merged since tag: 0" not in out
    assert st.BOUNDARY_UNRESOLVED in out
    assert "?" in out


def test_a_resolved_zero_says_it_is_a_measured_zero() -> None:
    out = "\n".join(st.render(
        boundary_state=st.BOUNDARY_RESOLVED,
        chosen={"name": "v0.31.0", "sha": "39372ab",
                "commit_date": "2026-08-09T17:13:43+02:00",
                "reachable": True, "objtype": "commit"},
        notes=[], rows=[], undated=[], count_text="0", count_state=st.COUNT_EXACT,
        fragments=(0, {}, ""),
        only_api=[], only_git=[], unattributed=[], sources=["default branch: master"],
    ))
    assert "v0.31.0" in out
    assert "39372ab" in out
    assert st.COUNT_EXACT in out


def test_the_contradiction_that_filed_the_issue_is_rendered_as_a_finding() -> None:
    """Zero merges beside seven fragments is not two numbers, it is a finding."""
    out = "\n".join(st.render(
        boundary_state=st.BOUNDARY_RESOLVED,
        chosen={"name": "v0.31.0", "sha": "39372ab",
                "commit_date": "2026-08-09T17:13:43+02:00",
                "reachable": True, "objtype": "commit"},
        notes=[], rows=[], undated=[], count_text="0", count_state=st.COUNT_EXACT,
        fragments=(7, {"fixed": 7}, ""),
        only_api=[], only_git=[], unattributed=[], sources=["default branch: master"],
    ))
    assert "CONTRADICTION" in out

def test_unknown_reachability_is_ambiguous_not_an_assumed_yes() -> None:
    """`git tag --merged` failing must not silently promote every tag to on-branch."""
    tags = [
        _tag("v0.31.0", "2026-08-09T17:13:43+02:00", reachable=None),
        _tag("v0.30.0", "2026-08-09T08:29:26+02:00", reachable=None),
    ]
    chosen, state, notes = st.select_tag(tags, "")
    assert chosen["name"] == "v0.31.0"
    assert state == st.BOUNDARY_AMBIGUOUS
    assert any("reachab" in n.lower() for n in notes)

# ---------------------------------------------------------------------------
# The cap survives being outranked, and the tag hint is useful
# ---------------------------------------------------------------------------

def test_a_full_page_says_so_even_when_the_count_state_is_unverified() -> None:
    """A reconciliation gap outranks the cap in count_state; it must not erase it."""
    assert "PAGE FULL" in st.page_note(page=3, limit=3)


def test_a_short_page_carries_no_cap_warning() -> None:
    assert "PAGE FULL" not in st.page_note(page=14, limit=100)


def test_the_cap_is_measured_on_the_page_not_on_what_survived_the_filter() -> None:
    """The page is what gh returned; `kept` is what the boundary filter left.

    A full page thinned to three rows is still a full page, and calling that
    number EXACT is the confident-wrong-number bug one layer down — the very
    thing this op exists to stop.
    """
    assert "PAGE FULL" in st.page_note(page=5, limit=5)
    state, text = st.count_state(kept=2, limit=5, undated=0, unreconciled=0,
                                 page=5)
    assert state == st.COUNT_LOWER_BOUND
    assert text == ">=2"


def test_page_defaults_to_the_kept_count_when_it_is_not_supplied() -> None:
    state, _text = st.count_state(kept=5, limit=5, undated=0, unreconciled=0)
    assert state == st.COUNT_LOWER_BOUND


# ---------------------------------------------------------------------------
# repo: targeting — half a target is worse than none
# ---------------------------------------------------------------------------

def test_a_repo_target_is_refused_rather_than_half_applied() -> None:
    """Only the PR list can follow `repo:`; the boundary and cross-check cannot.

    Measured before this refusal existed: `repo:.../claude-remember` +
    `gh-since-tag` printed claude-supertool's v0.31.0 as the boundary,
    claude-remember's merge count against it, and claude-supertool's fragment
    count — three numbers about two repositories under one header.
    """
    message = st.repo_target_refusal("Digital-Process-Tools/claude-remember")
    assert message
    assert "Digital-Process-Tools/claude-remember" in message


def test_no_repo_target_is_not_a_refusal() -> None:
    assert st.repo_target_refusal(None) == ""
    assert st.repo_target_refusal("") == ""


def test_the_unknown_tag_hint_names_the_NEWEST_tags_not_the_alphabetical_first() -> None:
    """`0.3.0, 0.3.1, 0.3.2` is the least useful eight this repo could offer."""
    tags = [_tag("0.3.%d" % n, "2026-04-17T20:%02d:00+02:00" % n) for n in range(9)]
    tags.append(_tag("v0.31.0", "2026-08-09T17:13:43+02:00"))
    _chosen, _state, notes = st.select_tag(tags, "v9.9.9")
    hint = " ".join(notes)
    assert "v0.31.0" in hint
    assert "0.3.0" not in hint
