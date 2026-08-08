"""radar's GitLab tier must refuse a token it could not apply (#961).

#939 rewired `gh-prs`, `gl-mrs` and radar's **GitHub** tier onto the shared
tokenizer in `presets/_filter_tokens.py`. The **GitLab** tier was left alone, so
`presets/watch/tiers/gl_mrs.py` still read:

    multi = mrs._parse_multi(arg)[0] if arg else {}
    return multi or default_filter()

`_parse_multi` returns `(filters, flags, unknown)`; `[0]` throws the third
element away. Two ways that widens the population radar watches:

  * `radar:milestne=x`          -> `{}` -> falls through to `default_filter()`,
                                   i.e. every open MR of mine.
  * `radar:author=@me,milestne=x` -> `{"author": ["@me"]}` — the typo'd key is
                                   dropped and the *author* board renders as
                                   the milestone one.

Worse here than at the op level. `gh-prs` printing an unfiltered board wastes a
read; this tier resolves a *population* and then spawns over it — `heal()` starts
a per-MR watcher for every iid, and `feed_scope()` names the discovery feed. A
silently widened scope therefore fires `mr_opened` for strangers' MRs. The
mr-feed poller already declines exactly this (`fetch_population` returns `None`
on an unapplied token, #939); the tier is the same shape and did not get it.

Three things are pinned, and the second and third are on the same line as the
first:

1. A token the tier cannot honour is refused, before any `glab` call and before
   anything is spawned.
2. A *known* key with a value that maps to nothing is the same defect wearing a
   recognised name: `state=mergd` emits no `--merged`, and glab's default is
   `opened`, so the merged board renders as the open one — and radar starts
   watching those open MRs.
3. The tier's vocabulary is its own. It is a strict *subset* of the op's
   (`iids` and `failed` are board shapes a radar board must not silently take,
   `per=` is a knob this tier reads from config and never from the arg) and a
   strict *superset* of the GitHub tier's (`glab mr list` has `--milestone`,
   `--source-branch` and `--target-branch`; `gh pr list` has none of them). Both
   directions are asserted, because "share the tokenizer" must not become
   "inherit the vocabulary" in either direction.
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


tier = _load("radar_gl_mrs_961", "presets/watch/tiers/gl_mrs.py")
gh_tier = _load("radar_gh_prs_961", "presets/watch/tiers/gh_prs.py")
mrs = _load("gitlab_mrs_961", "presets/gitlab/mrs.py")
defaults = _load("watch_defaults_961", "presets/watch/defaults.py")


# ---------------------------------------------------------------------------
# (1) a token the tier has never heard of
# ---------------------------------------------------------------------------

def test_a_typod_key_alone_refuses_instead_of_widening_to_the_default() -> None:
    """`radar:milestne=x` used to resolve to `default_filter()` — every open MR.

    The empty dict falls through `multi or default_filter()`, so the operator
    asked for one milestone and radar started watching their whole board.
    """
    with pytest.raises(tier.RadarError) as exc:
        tier.resolve_filter("milestne=x")
    assert "milestne" in str(exc.value)


def test_a_typod_key_beside_a_good_one_refuses_instead_of_being_dropped() -> None:
    """The quieter half: `multi` is truthy, so the default never enters.

    `author=@me,milestne=x` resolved to `{"author": ["@me"]}` and the board
    labelled itself `scope author=@me` — a scope line that is *true about the
    query* and false about the question, which is the hardest kind to notice.
    """
    with pytest.raises(tier.RadarError) as exc:
        tier.resolve_filter("author=@me,milestne=x")
    assert "milestne" in str(exc.value)


def test_a_bare_unknown_token_refuses() -> None:
    with pytest.raises(tier.RadarError, match="onlygreen"):
        tier.resolve_filter("onlygreen")


def test_the_refusal_names_every_bad_token_and_what_would_have_worked() -> None:
    with pytest.raises(tier.RadarError) as exc:
        tier.resolve_filter("onlygreen,milestne=x")
    msg = str(exc.value)
    assert "onlygreen" in msg and "milestne" in msg, (
        f"every unapplied token must be named, not just the first: {msg}"
    )
    for accepted in ("author", "reviewer", "label", "milestone", "state"):
        assert accepted in msg, f"the accepted filters must be listed: {msg}"
    assert "no flags" in msg.lower(), (
        f"a tier that accepts no flags must say so, not print an empty "
        f"list (#973): {msg}"
    )


def test_the_refusal_does_not_fuse_two_unapplied_tokens_into_an_invented_one() -> None:
    """The bug #939 hit on the GitHub tier's hand-rolled message.

    Two `", ".join(...)` calls abutting produced `'milestone=onlygreen'` — a
    `key=value` the caller never typed, whose value came from a different token.
    """
    with pytest.raises(tier.RadarError) as exc:
        tier.resolve_filter("milestne=x,onlygreen")
    msg = str(exc.value)
    assert "milestne=onlygreen" not in msg, (
        f"the two unapplied tokens must not fuse into one invented token: {msg}"
    )


# ---------------------------------------------------------------------------
# (2) a known key whose value maps to nothing — same line, different bug
# ---------------------------------------------------------------------------

def test_an_unmappable_state_refuses_rather_than_rendering_the_open_board() -> None:
    """`state=mergd` is in `_FILTER_KEYS`, so `unknown` is empty and it passes.

    `_build_list_cmd` looks the value up in `_STATE_FLAG`, finds nothing and
    emits no flag at all. glab's default is `opened`, so radar renders the open
    board under a `scope state=mergd` label and heals watchers onto it.
    """
    assert mrs._parse_multi("state=mergd")[2] == [], (
        "state= is a key the tokenizer forwards, or this test proves nothing"
    )
    assert "--merged" not in mrs._build_list_cmd({"state": "mergd"}, 50), (
        "the value must genuinely map to no flag, or there is nothing to refuse"
    )
    with pytest.raises(tier.RadarError) as exc:
        tier.resolve_filter("state=mergd")
    msg = str(exc.value)
    assert "mergd" in msg and "merged" in msg, (
        f"the refusal must name the bad value and the accepted ones: {msg}"
    )


@pytest.mark.parametrize("state", sorted(mrs._STATES))
def test_every_state_the_op_can_map_is_still_accepted(state: str) -> None:
    assert tier.resolve_filter(f"author=@me,state={state}") == {
        "author": ["@me"], "state": [state],
    }


# ---------------------------------------------------------------------------
# (3) the tier's vocabulary is its own, in both directions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("shape", ["iids", "failed"])
def test_a_board_shape_the_op_offers_is_refused_here(shape: str) -> None:
    """`iids` and `failed` narrow a board; a radar board must not silently narrow.

    `iids` is the worst of the two — it is the payload the feed hands to the
    watcher spawner.
    """
    assert shape in mrs._FLAGS, (
        f"{shape!r} must be a flag the op accepts, or this test proves nothing"
    )
    assert mrs._parse_multi(shape)[2] == [], "the op accepts it"
    with pytest.raises(tier.RadarError, match=shape):
        tier.resolve_filter(shape)


def test_per_is_refused_because_this_tier_reads_the_page_size_from_config() -> None:
    """`live_open_mrs` passes `cfg["per_page"]`, never the arg's `per=`.

    Accepting it would be the defect one level down: a caller who asked for a
    different window, told nothing about not getting it.
    """
    assert "per" in mrs._FILTER_KEYS and mrs._parse_multi("per=10")[2] == []
    with pytest.raises(tier.RadarError, match="per"):
        tier.resolve_filter("per=10")


def test_the_tier_vocabulary_is_a_strict_subset_of_the_ops() -> None:
    assert tier.KNOWN_FLAGS < mrs._FLAGS, "the tier's flag set is a strict subset"
    assert tier.KNOWN_FILTERS < mrs._FILTER_KEYS, "and so is its filter set"


def test_the_tier_vocabulary_is_a_strict_superset_of_the_github_tiers() -> None:
    """The half that is not mechanical: GitLab's filter surface is genuinely wider.

    `glab mr list` has `--milestone`, `--source-branch` and `--target-branch`;
    `gh pr list` has none of them. Copying the GitHub tier's `KNOWN_FILTERS`
    would refuse three filters this tier can actually apply — the opposite
    error, and just as wrong.
    """
    assert gh_tier.KNOWN_FILTERS < tier.KNOWN_FILTERS
    for key in ("milestone", "source-branch", "target-branch"):
        assert key not in gh_tier.KNOWN_FILTERS
        assert key in tier.KNOWN_FILTERS
        assert mrs._FILTER_FLAG[key] in mrs._build_list_cmd({key: "v"}, 50), (
            f"{key} must reach the argv, or accepting it here is a silent drop"
        )
    assert tier.resolve_filter("milestone=v19,source-branch=x,target-branch=master") == {
        "milestone": ["v19"], "source-branch": ["x"], "target-branch": ["master"],
    }


def test_every_accepted_filter_key_actually_reaches_the_argv() -> None:
    """The subset must be justified key by key, not merely be a subset.

    A key in `KNOWN_FILTERS` that `_build_list_cmd` ignores is exactly the
    silent drop this file is about, moved one level up.
    """
    for key in tier.KNOWN_FILTERS:
        cmd = mrs._build_list_cmd({key: "opened" if key == "state" else "v"}, 50)
        flag = "--merged" if key == "state" else mrs._FILTER_FLAG[key]
        assert key == "state" or flag in cmd, f"{key} never reaches glab"


# ---------------------------------------------------------------------------
# refusing must not become a new false negative
# ---------------------------------------------------------------------------

def test_the_bare_default_still_resolves() -> None:
    assert tier.resolve_filter("") == tier.default_filter()
    assert tier.resolve_filter() == tier.default_filter()


def test_the_repo_wide_default_filter_is_not_refused() -> None:
    """`defaults.DEFAULT_FILTER` is what radar, the feed and watch-mine.sh share.

    A refusal there would break every "watch everything of mine" flow at once.
    """
    assert mrs._parse_multi(defaults.DEFAULT_FILTER)[2] == []
    assert tier.resolve_filter(defaults.DEFAULT_FILTER) == tier.default_filter()


@pytest.mark.parametrize("arg_str", [
    "author=@me",
    "author=@me,state=opened",
    "author=@me,author=other",
    "reviewer=@me",
    "assignee=@me",
    "label=bug",
    "milestone=v18.9",
    "source-branch=x,target-branch=master",
    " author = me ",
])
def test_arg_strings_that_worked_before_still_work(arg_str: str) -> None:
    assert tier.resolve_filter(arg_str)


def test_a_repeated_key_still_accumulates() -> None:
    """GitLab takes one author per query, so the union is the population."""
    assert tier.resolve_filter("author=a,author=b") == {"author": ["a", "b"]}


def test_a_value_the_backend_rejects_is_not_this_tiers_business() -> None:
    """`milestone=nosuchmilestone` is forwarded — an empty board there is true."""
    assert tier.resolve_filter("milestone=nosuchmilestone") == {
        "milestone": ["nosuchmilestone"],
    }


# ---------------------------------------------------------------------------
# the consequence that makes this worse than the op-level bug
# ---------------------------------------------------------------------------

def test_a_refused_arg_calls_no_glab_and_spawns_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point: radar resolves a population and then *spawns over it*.

    `heal()` starts a per-MR watcher per iid and `feed_scope()` names the
    discovery feed. Both must be unreachable when the scope could not be
    resolved — refusing after the fleet has been widened would fix the report
    and keep the bug.
    """
    def _must_not_run(*_a: object, **_k: object) -> None:
        raise AssertionError("glab must not be called for a scope nobody applied")

    def _must_not_spawn(*_a: object, **_k: object) -> None:
        raise AssertionError("nothing may be spawned for a scope nobody applied")

    monkeypatch.setattr(tier.mrs, "_run", _must_not_run)
    monkeypatch.setattr(tier.dispatcher, "start_poller", _must_not_spawn)

    with pytest.raises(tier.RadarError, match="milestne"):
        tier.radar_report({"_arg": "author=@me,milestne=x", "_watch": _must_not_spawn})


def test_radar_surfaces_the_refusal_on_stderr_and_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """The judgment call, pinned: refuse loudly, do not decline quietly.

    Radar runs unattended, so a tier that cannot resolve its scope must reach
    the operator. `tier_reports` already catches per tier — the refusal lands in
    the `failures` channel (stderr, exit 1) and every *other* tier still renders
    its board. Both halves are the reason `RadarError` is the right answer here
    rather than the mr-feed poller's `None`.
    """
    radar = _load("radar_961", "presets/watch/radar.py")
    monkeypatch.setenv(radar.TIERS_ENV, '{"gl-mrs": {}}')
    monkeypatch.setattr(radar.dispatcher, "reap_duplicate_pollers", lambda: [])

    code = radar.main(["radar", "author=@me,milestne=x"])
    captured = capsys.readouterr()
    assert code == 1, "an unresolvable scope must not leave radar exiting 0"
    assert "milestne" in captured.err, captured.err
    assert "milestne" not in captured.out, (
        f"the refusal belongs on the cannot-tell channel, not the board: "
        f"{captured.out}"
    )


def test_one_refusing_tier_does_not_cost_a_working_tier_its_board(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refusal is per tier, so the rest of the board still renders."""
    radar = _load("radar_961_two", "presets/watch/radar.py")
    monkeypatch.setenv(radar.TIERS_ENV, '{"gl-mrs": {}}')

    sentinel = ["other tier line"]

    class _Fake:
        RADAR_OPTIONS: set[str] = set()
        RADAR_QUIET_DEFAULT = False

        @staticmethod
        def radar_report(_options: dict) -> tuple[list[str], bool]:
            return sentinel, True

    real = radar._tier_module
    monkeypatch.setattr(
        radar, "_tier_module",
        lambda name: _Fake if name == "other" else real(name))
    monkeypatch.setattr(radar, "read_tiers",
                        lambda raw=None: ({"gl-mrs": {}, "other": {}}, []))
    monkeypatch.setattr(radar.dispatcher, "reap_duplicate_pollers", lambda: [])

    lines, all_ok, failures = radar.tier_reports("author=@me,milestne=x")
    assert sentinel[0] in lines, "a refusing tier must not silence a working one"
    assert not all_ok
    assert any("milestne" in f for f in failures), failures


def test_radar_state_declines_in_place_rather_than_raising() -> None:
    """Read-only inspection keeps rendering, the way the GitHub tier does.

    `radar --state` exists so looking costs nothing. A raise there would make
    the one view that never spawns the one view you cannot open.
    """
    out = tier.radar_state({"_arg": "author=@me,milestne=x"})
    assert out and "REFUSED" in out[0], out
    assert "milestne" in out[0], out
