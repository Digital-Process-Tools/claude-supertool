"""`gh-since-tag` folds into `gh-prs:merged-since=TAG` (#1405).

## Why a tag name, and not the date the op already took

`merged-since=` shipped in #1411 taking an ISO date or instant. It cannot carry
an instant: supertool splits an op argument on ':', so
`merged-since=2026-08-11T20:57:19+02:00` is three argv segments and the value is
gone before any filter is parsed. The op's own refusal says there is no escape.
The only typable spelling was a bare date — midnight UTC — and against v0.35.0's
real boundary that is not a rounding error:

    merged:>2026-08-11T00:00:00+00:00  ->  75 PRs
    merged:>2026-08-11T18:57:19+00:00  ->  20 PRs

A tag name has no ':' in it. It is the only colon-free spelling of a
second-precision boundary, which is why the fold is expressed this way rather
than by widening the date parser.

## The acceptance bar

`gh-prs` is the most-called op here — radar's tier, every triage tick. It was one
`gh` call; on the boundary slice it now also reads tags, walks `git log` and
scans `changelog.d/`. This op has already shipped a narrowed population that
rendered like a complete one (`author=@me`, #1207, then #1230 when radar
inherited it), so the two loads here are:

1. A caller who does not pass `merged-since=` gets today's behaviour, and no
   local read happens at all.
2. A caller who does can tell **from the output alone** which of the three
   conditional reads ran. A footer silent about a check that did not run is
   indistinguishable from one where the check passed.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PRESETS = Path(__file__).parent.parent / "presets"
sys.path.insert(0, str(PRESETS))
sys.path.insert(0, str(PRESETS / "github"))

import _filter_tokens  # noqa: E402
import prs  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "github_release_gate_fold", PRESETS / "github" / "_release_gate.py")
assert _spec is not None and _spec.loader is not None
rg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rg)

TAG_SHA = "a6c3a8bcf3e5db09e2f5b06cb54cc9187f2c5232"
TAG_DATE = "2026-08-11T15:34:34+02:00"
BRANCH = "refs/remotes/origin/master"


def _tags_line(name="v0.34.0", date=TAG_DATE, sha=TAG_SHA):
    return "\t".join([name, "commit", "", date, "", sha])


def _fake_git(*, tags_out, merged_out="", subjects_out="", subjects_ok=True):
    """Stand in for `_release_gate._run` — every git read this module makes."""
    def run(argv, timeout):
        if argv[:2] == ["git", "rev-parse"]:
            return True, BRANCH + "\n", ""
        if argv[:2] == ["git", "for-each-ref"]:
            return True, tags_out, ""
        if argv[:3] == ["git", "tag", "--merged"]:
            return True, merged_out, ""
        if argv[:2] == ["git", "log"]:
            if not subjects_ok:
                return False, "", "fatal: bad revision"
            return True, subjects_out, ""
        return True, "", ""
    return run


# ---------------------------------------------------------------------------
# 1. The value: a tag name is accepted, a date is still a date
# ---------------------------------------------------------------------------

def test_merged_since_accepts_a_tag_name_as_well_as_an_instant() -> None:
    assert prs._VALUE_DOMAINS["merged-since"] is _filter_tokens.ISO_INSTANT_OR_TAG


def test_a_tag_shaped_value_is_read_as_a_ref_and_a_date_shaped_one_is_not() -> None:
    for ref in ("v0.34.0", "0.3.2", "v1.0.0-rc1", "release-2026"):
        assert _filter_tokens.looks_like_ref(ref) is True, ref
    for not_ref in ("2026-08-11", "2026-13-45", "", "  ", "-v1", "a b", "a:b"):
        assert _filter_tokens.looks_like_ref(not_ref) is False, not_ref


def test_a_broken_date_is_refused_as_a_date_not_hunted_for_as_a_tag() -> None:
    """`2026-13-45` is a typo, and `no such tag` is the wrong sentence for it."""
    bad = prs._bad_values({"merged-since": "2026-13-45"})
    assert bad and bad[0][0] == "merged-since"


def test_the_refusal_names_the_tag_spelling_as_the_colon_free_route() -> None:
    """The old hint sent a caller to a bare date, which is midnight UTC."""
    assert "merged-since=TAG" in prs._COLON_HINT or "tag" in prs._COLON_HINT.lower()


# ---------------------------------------------------------------------------
# 2. Three states on the value — a filter may refuse, it may not pick
# ---------------------------------------------------------------------------

def test_a_resolvable_tag_becomes_a_second_precision_instant() -> None:
    rg._run = _fake_git(tags_out=_tags_line() + "\n", merged_out="v0.34.0\n")
    boundary = rg.resolve_boundary("v0.34.0")
    assert boundary.state == rg.BOUNDARY_RESOLVED
    assert boundary.stamp == "2026-08-11T13:34:34+00:00"
    assert boundary.sha == TAG_SHA
    assert boundary.refusal == ""


def test_an_ambiguous_boundary_refuses_and_names_both_candidates() -> None:
    """Two version tags at one instant on different commits. Both defensible."""
    rg._run = _fake_git(
        tags_out=_tags_line() + "\n" + _tags_line("v0.34.1", sha="b" * 40) + "\n",
        merged_out="v0.34.0\nv0.34.1\n")
    boundary = rg.resolve_boundary("")
    assert boundary.state == rg.BOUNDARY_AMBIGUOUS
    assert boundary.refusal
    assert "v0.34.1" in boundary.refusal


def test_an_unresolvable_tag_refuses_and_never_substitutes_the_newest() -> None:
    rg._run = _fake_git(tags_out=_tags_line() + "\n", merged_out="v0.34.0\n")
    boundary = rg.resolve_boundary("v9.9.9")
    assert boundary.state == rg.BOUNDARY_UNRESOLVED
    assert "v9.9.9" in boundary.refusal
    assert boundary.stamp == ""


def test_the_refusal_points_at_this_op_not_at_the_retired_one() -> None:
    rg._run = _fake_git(tags_out=_tags_line() + "\n", merged_out="v0.34.0\n")
    boundary = rg.resolve_boundary("v9.9.9")
    assert "gh-since-tag" not in boundary.refusal


# ---------------------------------------------------------------------------
# 3. repo: beside a tag — the hazard relocates, it does not vanish
# ---------------------------------------------------------------------------

def test_a_tag_boundary_under_a_repo_target_is_refused_by_name() -> None:
    """Measured on the retired op: one repo's tag beside another's merge count."""
    err = prs._tag_target_conflict("v0.34.0", "Digital-Process-Tools/claude-remember")
    assert err
    assert "v0.34.0" in err
    assert "claude-remember" in err


def test_a_date_boundary_under_a_repo_target_is_fine() -> None:
    """Nothing local is read for a date, so the target is fully honoured."""
    assert prs._tag_target_conflict("2026-08-11", "owner/name") is None


def test_no_repo_target_means_no_conflict() -> None:
    assert prs._tag_target_conflict("v0.34.0", "") is None


# ---------------------------------------------------------------------------
# 4. Every conditional read states whether it ran
# ---------------------------------------------------------------------------

ROWS = [
    {"number": 1403, "title": "release: v0.34.0",
     "mergedAt": "2026-08-11T13:34:35Z", "mergeCommit": {"oid": TAG_SHA}},
    {"number": 1404, "title": "after", "mergedAt": "2026-08-11T16:00:00Z",
     "mergeCommit": {"oid": "f" * 40}},
]


def _boundary(**over):
    base = dict(state=rg.BOUNDARY_RESOLVED, tag={"name": "v0.34.0", "sha": TAG_SHA[:7],
                                                 "commit_date": TAG_DATE, "tag_date": ""},
                instant=rg.parse_instant(TAG_DATE), sha=TAG_SHA,
                stamp="2026-08-11T13:34:34+00:00", branch_ref=BRANCH,
                notes=[], sources=[], refusal="")
    base.update(over)
    return rg.Boundary(**base)


def test_the_cross_check_says_it_ran_and_agreed() -> None:
    rg._run = _fake_git(tags_out="", subjects_out="after (#1404)\n")
    kept, lines, _code = rg.assess(rows=ROWS, boundary=_boundary(), per_page=50,
                            fetched=2, narrowed_by=[], repo_targeted=False,
                            changelog_dir="/nonexistent")
    text = "\n".join(lines)
    assert [r["number"] for r in kept] == [1404]
    assert "cross-check: RAN" in text and "AGREED" in text
    assert "merged since tag: 1" in text
    assert "[" + rg.COUNT_EXACT + "]" in text


def test_the_boundary_row_is_named_rather_than_silently_dropped() -> None:
    rg._run = _fake_git(tags_out="", subjects_out="after (#1404)\n")
    _kept, lines, _code = rg.assess(rows=ROWS, boundary=_boundary(), per_page=50,
                             fetched=2, narrowed_by=[], repo_targeted=False,
                             changelog_dir="/nonexistent")
    text = "\n".join(lines)
    assert "#1403" in text
    assert "identity" in text


def test_a_narrowed_population_makes_the_cross_check_decline_by_name() -> None:
    """A role filter narrows the API side only, so any gap is the filter's."""
    rg._run = _fake_git(tags_out="", subjects_out="after (#1404)\n")
    _kept, lines, _code = rg.assess(rows=ROWS, boundary=_boundary(), per_page=50,
                             fetched=2, narrowed_by=["author"],
                             repo_targeted=False, changelog_dir="/nonexistent")
    text = "\n".join(lines)
    assert "cross-check: DID NOT RUN" in text
    assert "author" in text


def test_a_repo_target_makes_the_cross_check_decline_by_name() -> None:
    rg._run = _fake_git(tags_out="", subjects_out="after (#1404)\n")
    _kept, lines, _code = rg.assess(rows=ROWS, boundary=_boundary(), per_page=50,
                             fetched=2, narrowed_by=[], repo_targeted=True,
                             changelog_dir="/nonexistent")
    assert "cross-check: DID NOT RUN" in "\n".join(lines)


def test_a_cross_check_that_could_not_run_is_unverified_not_agreement() -> None:
    rg._run = _fake_git(tags_out="", subjects_ok=False)
    _kept, lines, _code = rg.assess(rows=ROWS, boundary=_boundary(), per_page=50,
                             fetched=2, narrowed_by=[], repo_targeted=False,
                             changelog_dir="/nonexistent")
    text = "\n".join(lines)
    assert rg.COUNT_UNVERIFIED in text


def test_a_changelog_dir_that_was_not_read_says_so_rather_than_counting_zero() -> None:
    rg._run = _fake_git(tags_out="", subjects_out="after (#1404)\n")
    _kept, lines, _code = rg.assess(rows=ROWS, boundary=_boundary(), per_page=50,
                             fetched=2, narrowed_by=[], repo_targeted=False,
                             changelog_dir="/nonexistent")
    text = "\n".join(lines)
    assert "changelog.d: NOT READ" in text
    assert "unreleased fragments: ?" in text


def test_the_contradiction_survives_the_fold(tmp_path) -> None:
    """Zero merges beside seven fragments is the render that found #1209.

    It only exists because ONE op holds both numbers; batched ops do not talk to
    each other. If the fold had dropped it, this is the test that would fail.
    """
    for n in range(7):
        (tmp_path / f"{1200 + n}.fixed.md").write_text("x", encoding="utf-8")
    rg._run = _fake_git(tags_out="", subjects_out="")
    _kept, lines, _code = rg.assess(rows=[], boundary=_boundary(), per_page=50,
                             fetched=0, narrowed_by=[], repo_targeted=False,
                             changelog_dir=str(tmp_path))
    text = "\n".join(lines)
    assert "CONTRADICTION" in text
    assert "unreleased fragments: 7" in text


def test_a_real_disagreement_still_refuses(tmp_path) -> None:
    """Narrowing the structural case must not silence the genuine one."""
    rg._run = _fake_git(tags_out="", subjects_out="")
    _kept, lines, _code = rg.assess(rows=ROWS, boundary=_boundary(), per_page=50,
                             fetched=2, narrowed_by=[], repo_targeted=False,
                             changelog_dir=str(tmp_path))
    text = "\n".join(lines)
    assert "RECONCILE" in text
    assert "#1404" in text
    assert rg.COUNT_UNVERIFIED in text


def test_a_full_page_is_still_a_lower_bound_under_the_boundary() -> None:
    rg._run = _fake_git(tags_out="", subjects_out="after (#1404)\n")
    _kept, lines, _code = rg.assess(rows=ROWS, boundary=_boundary(), per_page=2,
                             fetched=2, narrowed_by=[], repo_targeted=False,
                             changelog_dir="/nonexistent")
    assert "PAGE FULL" in "\n".join(lines)


# ---------------------------------------------------------------------------
# 5. The acceptance bar — the default board is untouched
# ---------------------------------------------------------------------------

def test_a_call_without_a_boundary_builds_exactly_the_argv_it_built_before() -> None:
    cmd = prs._build_list_cmd({}, 50)
    assert cmd[:5] == ["gh", "pr", "list", "--json", prs._LIST_FIELDS]
    assert "--search" not in cmd
    assert "statusCheckRollup" in cmd[4]


def test_the_board_field_set_did_not_grow_a_merge_commit() -> None:
    """The identity read is the boundary slice's cost, not every caller's."""
    assert "mergeCommit" not in prs._LIST_FIELDS


def test_no_boundary_means_the_gate_is_never_consulted() -> None:
    """The load-bearing one. radar and every tick take this path."""
    assert prs._gate_plan({}) is None
    assert prs._gate_plan({"author": "@me", "state": "open"}) is None


def test_a_date_boundary_is_a_merged_slice_but_not_a_release_gate() -> None:
    plan = prs._gate_plan({"merged-since": "2026-08-11", "state": "merged"})
    assert plan is not None
    assert plan.is_tag is False


def test_a_tag_boundary_is_the_release_gate() -> None:
    plan = prs._gate_plan({"merged-since": "v0.34.0", "state": "merged"})
    assert plan is not None
    assert plan.is_tag is True


def test_a_count_that_is_not_exact_keeps_the_non_zero_exit() -> None:
    """The gate's exit code is the release trigger's actual answer.

    `gh-since-tag` exited 0 only when the boundary was RESOLVED **and** the
    count EXACT, so a script could gate on it. The fold nearly dropped that: a
    board returns 0, and `merged since tag: 5 (UNVERIFIED)` would have exited 0
    beside it — the strongest available statement of "go" attached to a number
    the tool has just said it cannot verify. Found by review, not by me.

    `gh-prs` still exits 0 for every ordinary board; only the boundary slice
    carries a verdict, because only it has one.
    """
    # Both sources agreed and the page was not full: a trigger input.
    rg._run = _fake_git(tags_out="", subjects_out="after (#1404)\n")
    _kept, _lines, code = rg.assess(
        rows=ROWS, boundary=_boundary(), per_page=50, fetched=2,
        narrowed_by=[], repo_targeted=False, changelog_dir="/nonexistent")
    assert code == 0

    # The cross-check declined, so the count is UNVERIFIED and must not exit 0.
    _kept, lines, code = rg.assess(
        rows=ROWS, boundary=_boundary(), per_page=50, fetched=2,
        narrowed_by=["author"], repo_targeted=False,
        changelog_dir="/nonexistent")
    assert rg.COUNT_UNVERIFIED in "\n".join(lines)
    assert code == 1


def test_an_ambiguous_boundary_could_never_reach_a_zero_exit() -> None:
    """It refuses before any board is fetched, so there is nothing to exit 0 on."""
    rg._run = _fake_git(
        tags_out=_tags_line() + "\n" + _tags_line("v0.34.1", sha="b" * 40) + "\n",
        merged_out="v0.34.0\nv0.34.1\n")
    boundary = rg.resolve_boundary("")
    assert boundary.state == rg.BOUNDARY_AMBIGUOUS
    assert boundary.stamp == ""


def test_the_narrowing_keys_are_derived_from_the_filter_set_not_listed() -> None:
    """The cross-check is only valid over an unnarrowed population.

    If a filter key is added to `_FILTER_KEYS` and not to the narrowing set, the
    cross-check runs against a board that key has narrowed and reports `RAN and
    AGREED` about two populations that were never comparable — a check that
    reports a verdict it had no standing to reach, which is this codebase's own
    defect class inside the guard written for it.

    So the set is computed as the complement of the three keys that do NOT
    narrow the population, and this test pins that complement rather than the
    list. Adding a key goes red here until someone decides which side it is on.
    """
    assert prs._NON_NARROWING_KEYS == {"state", "per", "merged-since"}
    assert prs._NARROWING_KEYS == prs._FILTER_KEYS - prs._NON_NARROWING_KEYS
    assert prs._NARROWING_KEYS == {"author", "assignee", "reviewer", "label"}


def test_a_date_boundary_says_the_gate_did_not_apply_rather_than_staying_silent() -> None:
    lines = rg.not_applicable_note()
    text = "\n".join(lines)
    assert "NOT APPLICABLE" in text
    assert "tag" in text


# ---------------------------------------------------------------------------
# 6. The retired op still answers, rather than becoming an unknown token
# ---------------------------------------------------------------------------

def test_gh_since_tag_is_gone_from_the_registry_as_a_working_op() -> None:
    import json
    registry = json.loads((PRESETS / "github.json").read_text(encoding="utf-8"))
    entry = registry["ops"]["gh-since-tag"]
    assert entry["description"].startswith("RETIRED")
    assert "merged-since=" in entry["description"]
