"""#1829 -- eight files hand-rolled the whole-config opt-out `with_preset_op` refuses.

`tests/conftest.py`'s autouse `_disable_rtk_and_config` hands every test
`supertool._CONFIG = {}`. `with_preset_op` (#1812) is the opt-in for *one*
route and is deliberately not the opt-in for the whole registry: a test asking
for `git-commit` must not silently acquire `gh-pr` as well, or every other op's
presence in that test becomes an accident of the fixture.

Eight files wanted the other thing -- this repo's own config, presets resolved
through the real loader -- and each rediscovered it. This file pins the second
fixture, `shipped_config`, and pins it in both directions: that it really
installs the preset-derived registry, and that a checkout whose manifests
cannot answer is a **failure** rather than a skip.

The failure half is the subtle one, and it is not the assertion the obvious
implementation writes. `.supertool.json` declares eight ops **directly**, so
`assert config["ops"]` stays true with every single `presets/*.json` deleted --
a guard that cannot observe the condition it names, which is this repository's
own defect class inside the guard against it. The signal `_merge_presets`
actually emits is `_preset_warnings`, and that is what the refusal reads.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import supertool

from conftest import _load_shipped_config
# The same helper, not a second copy of it: `pytest.raises(AssertionError)`
# would let a `Skipped` through, which is the exact mutation these tests exist
# to catch (#1812).
from test_with_preset_op_1812 import refusal


REPO_ROOT = Path(__file__).parent.parent

#: An op that exists only because a preset manifest was merged. Read off the
#: manifest rather than typed, so a rename reddens this file instead of
#: quietly making every assertion below vacuous.
PRESET_OP = "git-commit"


def _project(root: Path, config: dict, presets: "dict | None" = None) -> Path:
    """A directory carrying `.supertool.json`, and optionally its own presets.

    Naming a *real* preset here would not simulate a broken checkout.
    `_supertool._find_preset_file` resolves a name in three places -- the
    project, `~/.config/supertool/presets/`, then the **supertool install
    directory** -- so a throwaway project declaring `presets: ["git"]` loads
    this repository's own `presets/git.json` and merges perfectly. The first
    draft of this file asserted a refusal against exactly that and got a green
    merge instead; the names below are ones no directory in that chain
    declares.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / ".supertool.json").write_text(
        json.dumps(config), encoding="utf-8")
    for name, data in (presets or {}).items():
        (root / "presets").mkdir(exist_ok=True)
        (root / "presets" / f"{name}.json").write_text(
            json.dumps(data), encoding="utf-8")
    return root


# --- the trap, so nothing below is asserting into a void --------------------

def test_a_preset_op_is_absent_until_a_test_asks_for_the_shipped_config():
    """The positive control for every "the fixture installed it" claim below.

    Without this, `test_shipped_config_installs_the_preset_derived_registry`
    would pass just as happily if `shipped_config` did nothing at all.
    """
    assert supertool._CONFIG == {}, (
        "the autouse config reset did not hold -- some earlier test in this "
        "worker leaked config, and every claim in this file is untrustworthy")
    assert PRESET_OP not in (supertool._load_config().get("ops") or {})


# --- the opt-in works -------------------------------------------------------

def test_shipped_config_installs_the_preset_derived_registry(shipped_config):
    """The whole registry, which is precisely what `with_preset_op` withholds."""
    ops = supertool._load_config().get("ops") or {}
    assert PRESET_OP in ops, sorted(ops)
    assert ops[PRESET_OP]["cmd"], "the entry came through unresolved"
    # `{path}` is resolved by `_merge_presets` to the preset's own directory.
    # A config handed over unmerged would still carry the placeholder, and
    # every consumer would be reading a cmd the dispatcher cannot spawn.
    assert "{path}" not in ops[PRESET_OP]["cmd"]


def test_shipped_config_is_the_repo_config_and_says_where_it_came_from(
        shipped_config):
    """`_CONFIG_PATH` is what the eight hand-rolled fixtures all set too --
    `op_ops` and the guard refusal quote it, so a fixture that left it at the
    autouse value would render a path belonging to another checkout."""
    assert supertool._CONFIG_CHECKED is True
    assert supertool._CONFIG_PATH == str(REPO_ROOT / ".supertool.json")
    assert shipped_config is supertool._CONFIG


def test_the_returned_config_carries_the_builtin_section_too(shipped_config):
    """Four of the eight consumers read `builtin-ops`, not `ops`."""
    assert shipped_config.get("builtin-ops"), "builtin-ops did not survive"


# --- and the subset fixture stays honest beside it --------------------------

def test_with_preset_op_still_installs_only_what_was_asked_for(with_preset_op):
    """The property #1829 must not spend to buy `shipped_config`.

    The two fixtures share the loader; they must not share the install. If
    sharing the read ever turns into sharing the registry, this goes red.
    """
    with_preset_op(PRESET_OP)
    assert set(supertool._load_config().get("ops") or {}) == {PRESET_OP}


# --- each caller gets its own copy ------------------------------------------

def test_a_consumer_mutating_the_config_cannot_poison_the_cache(
        shipped_config):
    """The cache is shared; the config handed out must not be.

    Asserted **inside one test** against the cache itself, deliberately, and
    not as a mutate-here/check-there pair of tests. That pair was the first
    shape of this and it is unsound: `pyproject.toml`'s `addopts` carries
    `-n auto` with no `--dist`, so xdist schedules by *item* rather than by
    file, and the two halves can land in different workers. The checking half
    would then read its own worker's untouched `lru_cache` and pass -- passing
    just as happily with `copy.deepcopy` deleted from the fixture, which is the
    single regression it exists to catch. A test that silently stops asserting
    is this repository's defect class wearing the costume of a passing suite.
    """
    from conftest import _shipped_config_cached

    shipped_config["ops"].pop(PRESET_OP, None)
    shipped_config["ops"]["poisoned-by-1829"] = {"cmd": "echo x"}

    cached = _shipped_config_cached()
    assert "poisoned-by-1829" not in cached["ops"], (
        "the fixture handed out the cached object itself, so one test's "
        "mutation is now every later test's config")
    assert PRESET_OP in cached["ops"], (
        "the fixture handed out the cached object itself -- a consumer's "
        "`pop` has removed a route from every later test in this worker")

    # The must-fire partner: the assertions above pass trivially if the cache
    # is empty or missing the op for some unrelated reason, so pin that the
    # thing being protected is really there and really shared.
    assert cached is _shipped_config_cached(), "the cache is not a cache"
    assert cached["ops"] is not shipped_config["ops"]


# --- it fails, and does not skip, when the manifests cannot answer ----------

def test_a_checkout_whose_presets_are_unreadable_fails_and_does_not_skip(
        tmp_path: Path):
    """The judgment #1812 argued for, applied to the whole-config fixture.

    `presets/*.json` are tracked files one directory from `conftest.py`, so a
    checkout that cannot read them is broken rather than under-provisioned. A
    skip here would report an environment quirk for a suite that has stopped
    testing the shipped registry entirely.
    """
    project = _project(tmp_path / "broken",
                       {"presets": ["absent-1829-a", "absent-1829-b"]})
    exc = refusal(_load_shipped_config, project)
    assert "absent-1829-a" in str(exc), exc
    assert "absent-1829-b" in str(exc), (
        f"only the first unresolved preset was named, so a checkout missing "
        f"several would under-report which: {exc}")
    assert "1829" in str(exc), exc


def test_the_refusal_survives_a_config_that_declares_its_own_ops(
        tmp_path: Path):
    """The assertion the obvious implementation gets wrong.

    This repo's `.supertool.json` declares eight ops directly, so
    `assert config["ops"]` is satisfied by the project's own entries with every
    preset manifest missing. A guard written that way reports a healthy
    registry for a checkout that resolved none of it -- the absence produced by
    the tool, read as an absence in the world.

    Nor is counting the op names the merge *added* enough, which is the second
    wrong answer and the subtler one. This repo's eight direct `ops` entries
    are exactly the keys `git`, `watch` and `dashboard` ship, so a name-set
    difference cannot see those three presets contributing at all. The guard
    reads `_op_sources` instead -- the provenance the loader stamps as it
    merges, which records the originating preset even for an op the project
    overrode -- and asks it per declared preset.
    """
    project = _project(
        tmp_path / "partial",
        {
            "presets": ["hollow-1829"],
            "ops": {"local-only": {"cmd": "echo x", "syntax": "local-only:X"}},
        },
        # Present and parseable, so `_merge_presets` emits no warning at all --
        # and contributes nothing, so the whole preset-derived registry is
        # missing while `config["ops"]` is still truthy. This is the arm the
        # other two refusals cannot reach.
        presets={"hollow-1829": {"ops": {}}},
    )
    exc = refusal(_load_shipped_config, project)
    assert "hollow-1829" in str(exc), exc
    assert "1829" in str(exc), exc


def test_a_preset_masked_entirely_by_a_project_override_is_still_seen(
        tmp_path: Path):
    """The blind spot in counting names, pinned so it cannot come back.

    Here the preset resolves, contributes an op, and that op's name is one the
    project also declares -- so `set(merged) - set(project_ops)` is empty and a
    name-counting guard calls the checkout healthy. It is not the same thing as
    a preset that contributed nothing, and the two must not be collapsed: the
    op the project declared merged key-by-key over a real shipped definition,
    which is a working route, so this one must **pass**.

    The partner below is the failing half. Together they are the only pair that
    tells `_op_sources` apart from a name-set difference.
    """
    project = _project(
        tmp_path / "masked",
        {"presets": ["masked-1829"], "ops": {"shared-op": {"timeout": 60}}},
        presets={"masked-1829": {"ops": {
            "shared-op": {"cmd": "echo x", "syntax": "shared-op:X"}}}},
    )
    cfg = _load_shipped_config(project)
    # The preset really was the source, and the project's partial override
    # merged over it rather than replacing it.
    assert cfg["_op_sources"]["shared-op"]["preset"] == "masked-1829"
    assert cfg["ops"]["shared-op"]["cmd"] == "echo x"
    assert cfg["ops"]["shared-op"]["timeout"] == 60


def test_the_must_fire_partner_the_real_checkout_resolves_cleanly():
    """The silence half of both refusals above.

    Without this, they would pass just as happily if `_load_shipped_config`
    refused every input it was ever handed -- including the real repository,
    which would make `shipped_config` unusable and these tests still green.
    """
    cfg = _load_shipped_config(REPO_ROOT)
    assert not cfg.get("_preset_warnings"), cfg.get("_preset_warnings")
    assert PRESET_OP in (cfg.get("ops") or {})


def test_a_config_declaring_no_presets_at_all_is_also_a_refusal(
        tmp_path: Path):
    """`presets: []` emits no warning, so `_preset_warnings` alone cannot see
    it -- and it resolves exactly zero preset ops, which is the same broken
    state wearing a different costume. Both arms are needed; neither covers
    the other."""
    project = _project(tmp_path / "empty", {"presets": []})
    exc = refusal(_load_shipped_config, project)
    assert "1829" in str(exc), exc
