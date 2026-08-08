"""Both radar tiers accept tokens they never apply (#973).

Two instances of this repo's house defect, one per tier, and they live in the
same two files:

1. `nopipe` is accepted by both tiers and honoured by neither. `gh_prs`
   bound `filters, _flags` and threw the flags away; `gl_mrs.resolve_filter`
   never returned them at all. `radar:nopipe` exited 0 and the board was
   enriched anyway.
2. The GitHub tier had no value-domain check. `radar:state=opne` passed the
   key check — `state` IS a known filter — emitted no `--state`, and the
   default board rendered as the filtered one. The quieter half of #939's
   defect, and the half #961 only closed on the GitLab side.

Both are resolved by **refusing**, not by honouring, and the two halves of that
decision are different arguments:

* On the GitHub tier honouring `nopipe` is not even expressible. `nopipe` on
  the `gh-prs` op skips review-thread enrichment, and `live_open_prs` already
  skips it unconditionally — so a tier that "honoured" the flag would be
  byte-identical to one that ignored it. That is the no-op the issue forbids
  documenting.
* On the GitLab tier honouring it is expressible and wrong. The board's
  verdict, its drift check and its heal decision are all read off the pipeline
  enrichment `nopipe` would remove, so what comes back is not a cheaper board,
  it is a board with no answer in it.

So the flag joins `iids` and `failed` in the category #939/#961 already
established: board *shapes* and enrichment knobs the op offers and a radar
tier must not silently take. The symmetry those two issues fought for is kept
— neither tier accepts any flag now, rather than one tier diverging.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


prs = _load("github_prs_973", "presets/github/prs.py")
mrs = _load("gitlab_mrs_973", "presets/gitlab/mrs.py")
tokens = _load("filter_tokens_973", "presets/_filter_tokens.py")
gh_tier = _load("radar_gh_prs_973", "presets/watch/tiers/gh_prs.py")
gl_tier = _load("radar_gl_mrs_973", "presets/watch/tiers/gl_mrs.py")


# ---------------------------------------------------------------------------
# 1. a flag neither tier applies is refused, on both tiers
# ---------------------------------------------------------------------------

def test_the_github_tier_never_did_the_thing_nopipe_turns_off() -> None:
    """The premise of refusing rather than honouring, stated as a test.

    If `live_open_prs` ever grows the review-thread enrichment `nopipe` names,
    this assertion is the one that should send someone back to the issue: the
    refusal below would then be discarding a request the tier COULD honour.
    """
    assert "nopipe" in prs._FLAGS, "or the op no longer offers the flag at all"
    source = (_ROOT / "presets" / "watch" / "tiers" / "gh_prs.py").read_text(
        encoding="utf-8")
    assert "prs._enrich(" not in source, (
        "the tier acquired the review-thread pass `nopipe` skips — honouring "
        "the flag is now expressible, so re-argue #973 rather than keeping the "
        "refusal"
    )


def test_the_gitlab_tier_by_contrast_does_exactly_what_nopipe_turns_off() -> None:
    """The other half of the decision, and why it is not the same argument.

    On this tier `nopipe` IS expressible — `live_open_mrs` runs the enrichment
    unconditionally. It is refused because what the flag would return is not a
    cheaper board but a board with no verdict in it: the pipeline status, the
    drift check and the heal decision all read off that pass.
    """
    assert "nopipe" in mrs._FLAGS
    source = (_ROOT / "presets" / "watch" / "tiers" / "gl_mrs.py").read_text(
        encoding="utf-8")
    assert "mrs._enrich(" in source


def test_the_github_tier_refuses_nopipe_instead_of_accepting_and_dropping_it() -> None:
    with pytest.raises(gh_tier.RadarError) as exc:
        gh_tier.resolve_filter("nopipe")
    assert "nopipe" in str(exc.value)


def test_the_gitlab_tier_refuses_nopipe_instead_of_accepting_and_dropping_it() -> None:
    with pytest.raises(gl_tier.RadarError) as exc:
        gl_tier.resolve_filter("nopipe")
    assert "nopipe" in str(exc.value)


def test_a_good_filter_beside_nopipe_is_refused_too_rather_than_half_applied() -> None:
    """The quieter shape. A refusal that dropped only the bad token would leave
    a board labelled with a scope that is true about the query and false about
    the question — #961's exact wording, on a flag instead of a key."""
    for tier in (gh_tier, gl_tier):
        with pytest.raises(tier.RadarError, match="nopipe"):
            tier.resolve_filter("author=@me,nopipe")


def test_neither_tier_accepts_any_flag_and_that_is_deliberately_symmetric() -> None:
    """#939 closed an asymmetry and #961 refused to re-open it. A tier that
    unilaterally kept `nopipe` would be that asymmetry a third time."""
    assert gh_tier.KNOWN_FLAGS == set()
    assert gl_tier.KNOWN_FLAGS == set()
    assert gh_tier.KNOWN_FLAGS < prs._FLAGS, "still a strict subset of the op's"
    assert gl_tier.KNOWN_FLAGS < mrs._FLAGS


def test_the_refusal_says_no_flags_rather_than_printing_an_empty_list() -> None:
    """`Flags: .` is an error message that names what is wrong and not what to
    do — the thing `unknown_error` exists to avoid."""
    for tier in (gh_tier, gl_tier):
        with pytest.raises(tier.RadarError) as exc:
            tier.resolve_filter("nopipe")
        msg = str(exc.value)
        assert "no flags" in msg.lower(), msg
        assert "Flags: ." not in msg and "flags: ." not in msg, msg


def test_the_github_tier_stops_handing_back_a_flag_set_nobody_reads() -> None:
    """`radar_report` bound `filters, _flags` and discarded the second half.
    An always-empty channel is one a future caller re-populates and re-drops,
    so it goes rather than being documented as always empty. `gl_mrs` has
    returned the filter alone since #961; this is the same shape."""
    assert gh_tier.resolve_filter("author=@me") == {"author": "@me"}
    assert gh_tier.resolve_filter("") == {}


# ---------------------------------------------------------------------------
# 2. the GitHub tier gets the value-domain check #961 gave the GitLab one
# ---------------------------------------------------------------------------

def test_a_misspelled_state_survives_every_check_the_tier_had_before() -> None:
    """Proof the case is real rather than already covered: the key check passes
    and the argv builder is the only party that knows the request was dropped."""
    _filters, _flags, unknown = tokens.parse(
        "state=opne", gh_tier.KNOWN_FILTERS, gh_tier.KNOWN_FLAGS)
    assert unknown == [], "the key IS known — no unknown-token check can catch it"
    assert "--state" not in prs._build_list_cmd({"state": "opne"}, 50), (
        "and the flag never reaches gh, so the default board answers instead"
    )


def test_the_github_tier_refuses_a_state_value_it_cannot_map() -> None:
    with pytest.raises(gh_tier.RadarError) as exc:
        gh_tier.resolve_filter("state=opne")
    msg = str(exc.value)
    assert "opne" in msg
    for accepted in prs._STATES:
        assert accepted in msg, f"the accepted values must be named: {msg}"


def test_the_value_domain_is_the_ops_own_and_not_a_second_copy_of_it() -> None:
    """Two hand-written lists of GitHub's PR states is how they disagree."""
    assert gh_tier.VALUE_DOMAINS == {"state": prs._STATES}


@pytest.mark.parametrize("state", sorted(prs._STATES))
def test_every_state_the_op_maps_is_still_accepted_here(state: str) -> None:
    assert gh_tier.resolve_filter(f"author=@me,state={state}") == {
        "author": "@me", "state": state,
    }


def test_per_is_not_in_the_tiers_value_domain_because_it_is_refused_outright() -> None:
    """The op checks `per` as a positive integer. This tier refuses the key
    itself — `live_open_prs` reads the page size from config — so a value
    domain for it would be a check on a token that can never arrive."""
    assert "per" not in gh_tier.KNOWN_FILTERS
    assert "per" not in gh_tier.VALUE_DOMAINS
    with pytest.raises(gh_tier.RadarError, match="per"):
        gh_tier.resolve_filter("per=10")


_ARGV_PROBE = {
    "author": ("someone", "--author"),
    "assignee": ("someone", "--assignee"),
    "label": ("bug", "--label"),
    "reviewer": ("someone", "--search"),
    "state": ("merged", "--state"),
}


def test_every_accepted_filter_key_actually_reaches_the_argv() -> None:
    """#961's assertion, copied to the tier it was written about but not for.

    A key in `KNOWN_FILTERS` that `_build_list_cmd` ignores is the silent drop
    this file is about, moved one level up — accepted here, dropped there, and
    the board renders as though it had been applied.
    """
    assert set(_ARGV_PROBE) == gh_tier.KNOWN_FILTERS, (
        "a filter key was added or removed without proving it reaches gh"
    )
    for key, (val, flag) in _ARGV_PROBE.items():
        assert flag in prs._build_list_cmd({key: val}, 50), f"{key} never reaches gh"


def test_state_open_emits_no_flag_and_is_still_honoured() -> None:
    """The one accepted value that legitimately reaches no argv: `open` is
    gh's own default, so emitting nothing IS applying it. Worth pinning next to
    the assertion above, which would otherwise look violated."""
    assert "--state" not in prs._build_list_cmd({"state": "open"}, 50)
    assert gh_tier.resolve_filter("state=open") == {"state": "open"}
